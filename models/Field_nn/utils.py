import pandas as pd
import torch
import torch.nn as nn

class highD():
    def __init__(self,
                    tracks_path = './data/raw/13_tracks.csv',
                    recordingMeta_path = './data/raw/13_recordingMeta.csv',
                    tracksMeta_path = './data/raw/13_tracksMeta.csv',
                    cache = False,
                    device='cuda'
                ) -> None:
        
        self.tracks_path = tracks_path
        self.recordingMeta_path = recordingMeta_path
        self.tracksMeta_path = tracksMeta_path
        self.device = device
        self.trackMeta = pd.read_csv(self.tracksMeta_path)
        self.track = pd.read_csv(self.tracks_path)
        self.recordingMeta = pd.read_csv(self.recordingMeta_path)

        index = self.trackMeta.loc[:]['drivingDirection'] == 1
        self.dir1_vid = list(self.trackMeta['id'][index])
        self.dir1_trackMeta = self.trackMeta[index]
        # self.dir1_ids = self.dir1_trackMeta['id']
        self.norm_value_center = torch.tensor([201.7, 25.5, -3, 0])
        self.norm_value_scaler = torch.tensor([435, 36.5, 85.6, 3.6])
        
    
    def gen_dset(self, ifnorm=True):
        data = self.track
        
        max_frame = data['frame'].max() + 1
        max_id = data['id'].max() + 1

        dset_s = torch.zeros([max_frame,max_id,5])
        if ifnorm:
            for i in range(len(data)):
                # if data['id'][i] not in self.dir1_vid: continue
                t = [data['id'][i],data['x'][i],data['y'][i],data['xVelocity'][i],data['yVelocity'][i]]
                value = torch.tensor(t)
                value[1:] = (value[1:] - self.norm_value_center)/self.norm_value_scaler
                dset_s[data['frame'][i]][data['id'][i]] = value
        else:
            for i in range(len(data)):
                # if data['id'][i] not in self.dir1_vid: continue
                t = [data['id'][i],data['x'][i],data['y'][i],data['xVelocity'][i],data['yVelocity'][i]]
                value = torch.tensor(t)
                dset_s[data['frame'][i]][data['id'][i]] = value
        
        self.set = dset_s.to(device=self.device)
    
    def frame(self,frameId):
        frame = self.set[frameId].to_dense()
        return frame[frame[:,0]>0]
    
    def frange(self,begin_frame=0,end_frame=125):
        esbf = []
        for i in range(begin_frame,end_frame):
            esbf.append(self.frame(i))
        return esbf
    
    def search_track(self,target_id,begin_frame,end_frame):
        track = []
        for i in range(begin_frame,end_frame+1):
            if torch.norm(self.set[i][target_id]) != 0:
                track.append(self.set[i][target_id].to_dense())
        return torch.stack(track,dim=1)
    
    def get_Tracks(self,meta_id,meta):
        '''
        get the tracks of meta as well as the neighbors at the end frames of the meta
        '''
        item = meta[meta_id]
        track = self.search_track(item[0],item[1],item[2]).transpose(0,1)

        nei_cid = self.frame_neighbor(item[0],item[2])[:,0]
        nei_track = self.search_track(nei_cid.long(),item[1],item[2]).transpose(0,1)

        return torch.concatenate((track.unsqueeze(1),nei_track),dim=1)

    def frame_neighbor(self,center_car,frameId):
        frame = self.frame(frameId=frameId)
        return frame[frame[:,0]!=center_car]

def get_data(item, 
             highD_data
             ):
    
    ego_id = item[0]
    # ego_cls = item[1:3]
    Ninfo = []
    trackMeta = highD_data.trackMeta
    for fid in item[4:]:
        frame = highD_data.frame(fid)
        frame_nei = frame[frame[:,0]!=ego_id]
        P = frame[frame[:,0]==ego_id,1:3]
        V = frame[frame[:,0]==ego_id,3:5]
        Pn = frame_nei[:,1:3]
        Vn = frame_nei[:,3:5]
        Idn = frame_nei[:,0]
        Cn = []
        for id in Idn:
            if trackMeta['class'][trackMeta['id']==id.item()].iloc[0]=='Truck':
                Cn.append([0,1])
            elif trackMeta['class'][trackMeta['id']==id.item()].iloc[0]=='Car':
                Cn.append([1,0])
        Cn = torch.tensor(Cn).to(highD_data.device)
        idx = [i.item() in highD_data.dir1_vid for i in Idn]

        Ninfo.append((P,V,Pn[idx],Vn[idx],Cn[idx],Idn[idx]))

        

    return Ninfo

class net(nn.Module):
    def __init__(self):
        super(net,self).__init__()
        # self.Er_net = self.gen_Er_net()
        self.leaky_rule = nn.LeakyReLU(0.1)
        self.sfm = nn.Softmax(dim=0)
        self.LaneMark = torch.tensor([13.55,17.45,21.12,24.91]).cuda()

        # Er part
        Er_basis_num = 8
        self.Er_Linear_sel = nn.Linear(in_features=4,out_features=4)
        self.Er_Linaer_map = nn.Linear(in_features=32+Er_basis_num*2,out_features=Er_basis_num)
        self.Er_Linaer_efc = nn.Linear(in_features=Er_basis_num,out_features=2)
        self.aug_sin0 = nn.Linear(in_features=4,out_features=Er_basis_num)

        # En part
        En_basis_num = 17
        self.aug_sin1 = nn.Linear(in_features=2,out_features=En_basis_num)
        self.En_mlp = nn.Sequential(*[
            nn.Linear(in_features=En_basis_num*2+80,out_features=256),
            nn.LeakyReLU(0.1),
            nn.Linear(in_features=256,out_features=128),
            nn.LeakyReLU(0.1),
            nn.Linear(in_features=128,out_features=2)
        ])
        self.En_Linear_weight = nn.Linear(in_features=6,out_features=1)

    def Er_net(self,ego_y):
        delta_y = torch.tensor(list(map(lambda x:x-ego_y, self.LaneMark))).cuda()
        x = self.sfm(self.Er_Linear_sel(delta_y))*delta_y
        x = torch.concat(self.auge(x))
        x = self.leaky_rule(self.Er_Linaer_map(x))
        x = self.Er_Linaer_efc(x)
        return x

    def En_net(self,Pn,Pego,Vn,Vego,Cn):
        Pego = Pego.repeat(len(Pn),1)
        Vego = Vego.repeat(len(Pn),1)
        dP = Pn - Pego
        dV = Vn - Vego
        dP_aug = torch.concat(self.auge(dP,cls=1),dim=1)
        dV_aug = torch.concat(self.auge(dV,cls=1),dim=1)

        Ninfo = torch.concat((Pego,Pn,dP,dP_aug,Vego,Vn,dV,dV_aug,Cn),dim=1)
        Ei = []
        for nei in Ninfo:
            Ei.append(self.En_mlp(nei))
        
        En = torch.stack(Ei)
        weight = self.sfm(self.En_Linear_weight(torch.concat((En,Pn,dP),dim=1)))
        En_out = sum(weight*En)
        
        return En_out

    def auge(self,data,cls=0):
        if cls==0:
            sin = torch.sin(self.aug_sin0(data)) + 1e-6
        elif cls==1:
            sin = torch.sin(self.aug_sin1(data)) + 1e-6
        sqr = data**2 + 1e-6
        cub = data**3 + 1e-6
        exp = torch.exp(data) + 1e-6

        inv = 1/(data + 1e-6)
        inv_sin = 1/sin
        inv_sqr = 1/sqr
        inv_cub = 1/cub
        inv_exp = 1/exp

        data_aug = [data,sin,sqr,cub,exp,inv,inv_sin,inv_sqr,inv_cub,inv_exp]

        return data_aug
    
    
    def layer_stack(self,dim_list):
        layers = []
        for dim_in, dim_out in zip(dim_list[:-2],dim_list[1:-1]):
            layers.append(nn.Linear(in_features=dim_in,out_features=dim_out))
            layers.append(self.leaky_rule)
        layers.append(nn.Linear(in_features=dim_list[-2],out_features=dim_list[-1]))
        return nn.Sequential(*layers)
    
    def forward(self,data_item):
        ego_y = data_item.ego_y

        Er = self.Er_net(ego_y)
        # En = self.En_net(Pn,Pego,Vn,Vego,Cn)
        pass