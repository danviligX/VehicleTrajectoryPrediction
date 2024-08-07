import torch
import torch.nn as nn

class xconfig:
    batch_size: int=32
    neighbors_num: int=8
    delta_time: float=0.02
    embd: int=16
    select_lane: list=[0,-1] # two borders of a highway road

class weightConstraint(object):
    def __init__(self, min_b:float=None, max_b:float=None):       
        self.max_b = max_b if max_b is not None else None
        self.min_b = min_b if min_b is not None else None
        
    def __call__(self,module:nn.Module):
        if hasattr(module,'weight'):
            w = module.weight.data
            w = w.clamp(min=self.min_b, max=self.max_b)
            module.weight.data = w

class monotonic_fun(nn.Module):
    def __init__(self, config:xconfig, increasing=False) -> None:
        super().__init__()
        self.in_linear = nn.Linear(1,config.embd)
        self.out_linear= nn.Linear(config.embd,1)
        self.increasing = increasing
    def forward(self,x:torch.Tensor):
        x = self.in_linear(x)
        x = torch.exp(x) if self.increasing else torch.exp(-x)
        x = self.out_linear(x)
        return x

class SFM(nn.Module):
    def __init__(self, config:xconfig) -> None:
        super().__init__()
        '''
        requires: frames with ego and neighbors information
        outputs: the force on ego, i.e. the accerleration
        '''
        self.config = config

        # clamp setting
        _clamp_non_negative = weightConstraint(min_b=0)

        # Variable setting
        self.recording_time = torch.zeros(config.batch_size, config.neighbors_num, 2)   # Memory of time delation

        # Parameters setting
        self.attr_destination_para = nn.Parameter(torch.relu(torch.rand(2)))
        self.effective_angle = nn.Parameter(torch.sigmoid(torch.rand(1)))

        # monotonic function
        self.repu_nei_f = monotonic_fun(config=config, increasing=False).apply(_clamp_non_negative)
        self.attr_nei_f = monotonic_fun(config=config, increasing=False).apply(_clamp_non_negative)
        self.repu_bor_f = monotonic_fun(config=config, increasing=False).apply(_clamp_non_negative)
        self.delation_time_f = monotonic_fun(config=config, increasing=False).apply(_clamp_non_negative)

    def attr_destination(self, ego:torch.Tensor, desired_velocity:torch.Tensor=None):
        '''
        N: batch size
        ego:Nx16: [ 'id',
                    'x',
                    'y',
                    'xVelocity',
                    'yVelocity',
                    'xAcceleration',
                    'yAcceleration',
                    'precedingId', the vehicle in front of ego, index=7
                    'followingId', the vehicle behind the ego
                    'leftPrecedingId',
                    'leftAlongsideId',
                    'leftFollowingId',
                    'rightPrecedingId',
                    'rightAlongsideId',
                    'rightFollowingId',
                    'laneId']
        desired_velocity:Nx2: ['x', 'y'], 
            which is equal to desired position in one time step
        '''
        if desired_velocity is None: desired_velocity = torch.stack((ego[:,3:5].norm(dim=-1),torch.zeros(len(ego))),dim=1)
        vx = self.attr_destination_para[1]*desired_velocity - ego[:,3:5]
        return (vx/self.attr_destination_para[0]).unsqueeze(1)

    def attr_repu_neighbors(self, ego:torch.Tensor, nei:torch.Tensor):
        '''
        nei:Nx8x16: 8 x type(ego)
        '''
        recording_time = self.recording_time
        config = self.config
        delta_time = config.delta_time
        # Calculate the distance
        r = nei[:,:,1:3] - ego[:,1:3].unsqueeze(1)
       
        temp = []
        for i in range(self.config.batch_size):
            # If neighbors exists
            temp.append((torch.isin(nei[i,:,0], ego[i,7:-1]))&(nei[i,:,0]!=0))

            # Time delation: store time delation into self.recording_time
            ifin = torch.isin(recording_time[i,:,0],nei[i,:,0])
            recording_time[i,ifin,1]+=1
            reamin = recording_time[i,ifin]
            ifnew = (torch.isin(nei[i,:,0],recording_time[i,:,0])==False)&(nei[i,:,0]!=0)
            new_m = torch.stack((nei[i,ifnew,0],torch.ones_like(nei[i,ifnew,0])),dim=1)
            recording_time[i] = torch.concat((reamin,new_m,torch.ones([config.neighbors_num-len(reamin)-len(new_m),2])))

        idx = torch.stack(temp)
        r[idx==False]=0 # Zerolize it if there is no neighbors
        r_norm = r.norm(dim=-1).unsqueeze(-1)
        f_attr = self.delation_time_f(self.recording_time[:,:,1,None]) * self.attr_nei_f(r_norm) *r/r_norm
        v_nei = nei[:,:,3:5]
        b = r_norm + (r + v_nei*delta_time).norm(dim=-1).unsqueeze(-1)**2 - (v_nei*delta_time).norm(dim=-1).unsqueeze(-1)**2
        b = (b**0.5)/2
        force_scale = self.repu_nei_f(b)
        f_repu = force_scale * r/r_norm

        # If there is no neighbor
        f_attr[idx==False]=0
        f_repu[idx==False]=0
        return f_attr, f_repu

    def repu_borders(self, ego:torch.Tensor, border:torch.Tensor):
        '''
        A special function for HighD data. This function only calculates the force from y-axis, since HighD data is about highway.
        border: dim=1: (4.) for highd_13 set
        '''
        select_lane = self.config.select_lane
        r = (ego[:,2,None] - border)[:,select_lane]
        r_norm = r.abs()
        f_repu = self.repu_bor_f(r_norm.unsqueeze(-1))*(r/r_norm).unsqueeze(-1)
        f_repu = torch.concat((torch.zeros_like(f_repu),f_repu),dim=-1)
        return f_repu

    def cal_force(self, ego:torch.Tensor, nei:torch.Tensor, border:torch.Tensor, desired_velocity:torch.Tensor=None):
        f_attr_destination = self.attr_destination(ego, desired_velocity)
        f_attr_nei, f_repu_nei = self.attr_repu_neighbors(ego, nei)
        f_repu_border = self.repu_borders(ego, border)

        e = ego[:,3:5].norm(dim=-1)
        f_attr_destination_clamped = self.angle_clamp(f_attr_destination, e)
        f_attr_nei_clamped = self.angle_clamp(f_attr_nei, e)
        f_repu_nei_clamped = self.angle_clamp(f_repu_nei, e)
        f_repu_border_clampled = self.angle_clamp(f_repu_border, e)

        f_destination = f_attr_destination_clamped.sum(dim=1)
        f_neighors = f_attr_nei_clamped.sum(dim=1) + f_repu_nei_clamped.sum(dim=1)
        f_border = f_repu_border_clampled.sum(dim=1)

        


    def angle_clamp(self, vector:torch.Tensor, target:torch.Tensor):
        repeat_num = vector.shape[1]
        index = torch.cosine_similarity(target.repeat(repeat_num,1,1).transpose(0,1),vector,dim=-1).abs() > self.effective_angle
        clamped_vector = torch.zeros_like(vector)
        clamped_vector[index] = vector[index]
        return clamped_vector

def main():
    pass

if __name__=="__main__": main()