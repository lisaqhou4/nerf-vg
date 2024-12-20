import os
from opt import get_opts
import torch
from collections import defaultdict

from torch.utils.data import DataLoader
from datasets import dataset_dict

# models
from models.nerf import *
from models.rendering import *

# optimizer, scheduler, visualization
from utils import *

# losses
from losses import loss_dict

# metrics
from metrics import *

# pytorch-lightning
from pytorch_lightning.callbacks import ModelCheckpoint
from pytorch_lightning import LightningModule, Trainer
from pytorch_lightning.loggers import TensorBoardLogger


class NeRFSystem(LightningModule):
    def __init__(self, hparams):
        super().__init__()
        self.save_hyperparameters(vars(hparams))

        self.validation_outputs = []
        self.loss = loss_dict['nerfw'](coef=1)

        self.models_to_train = []
        self.embedding_xyz = PosEmbedding(hparams.N_emb_xyz-1, hparams.N_emb_xyz)
        self.embedding_dir = PosEmbedding(hparams.N_emb_dir-1, hparams.N_emb_dir)
        self.embeddings = {'xyz': self.embedding_xyz,
                           'dir': self.embedding_dir}

        if hparams.encode_a:
            self.embedding_a = torch.nn.Embedding(hparams.N_vocab, hparams.N_a)  # 700 * 48
            self.embeddings['a'] = self.embedding_a
            self.models_to_train += [self.embedding_a]
        # if hparams.encode_t:
        #     self.embedding_t = torch.nn.Embedding(hparams.N_vocab, hparams.N_tau)
        #     self.embeddings['t'] = self.embedding_t
        #     self.models_to_train += [self.embedding_t]
        if hparams.encode_outfit:
            self.embedding_outfit = torch.nn.Embedding(hparams.N_outfit, hparams.N_a)  # 2 * 48
            self.embeddings['outfit'] = self.embedding_outfit
            self.models_to_train += [self.embedding_outfit]

        self.nerf_coarse = NeRF('coarse',
                                encode_outfit=hparams.encode_outfit,
                                in_channels_xyz=6*hparams.N_emb_xyz+3,
                                in_channels_dir=6*hparams.N_emb_dir+3)
        self.models = {'coarse': self.nerf_coarse}
        if hparams.N_importance > 0:
            self.nerf_fine = NeRF('fine',
                                  in_channels_xyz=6*hparams.N_emb_xyz+3,
                                  in_channels_dir=6*hparams.N_emb_dir+3,
                                  encode_appearance=hparams.encode_a,
                                  in_channels_a=hparams.N_a,
                                  encode_transient=hparams.encode_t,
                                  in_channels_t=hparams.N_tau,
                                  beta_min=hparams.beta_min,
                                  encode_outfit=hparams.encode_outfit, in_channels_o=hparams.N_a)
            self.models['fine'] = self.nerf_fine
        self.models_to_train += [self.models]

    def get_progress_bar_dict(self):
        items = super().get_progress_bar_dict()
        items.pop("v_num", None)
        return items

    def forward(self, rays, ts, outfit_code):
        """Do batched inference on rays using chunk."""
        B = rays.shape[0]
        results = defaultdict(list)
        # print("shape of rays:", rays.shape)
        # print("shape of ts:", ts.shape)
        # print("shape of outfit_code:", outfit_code.shape)

        for i in range(0, B, self.hparams.chunk):
            # print("i, i+self.hparams.chunk, B:", i, i+self.hparams.chunk, B)
            
            # print("outfit_code in the train.forward:", outfit_code[i:i+self.hparams.chunk])
            # print("shape of outfit_code in the train.forward:", outfit_code[i:i+self.hparams.chunk].shape)
            outfit_code = outfit_code.squeeze(0)
            # print("shape of outfit_code in the train.forward after squeeze:", outfit_code[i:i+self.hparams.chunk].shape)
            rendered_ray_chunks = \
                render_rays(self.models,
                            self.embeddings,
                            rays[i:i+self.hparams.chunk],
                            ts[i:i+self.hparams.chunk],
                            outfit_code[i:i+self.hparams.chunk], 
                            self.hparams.N_samples,
                            self.hparams.use_disp,
                            self.hparams.perturb,
                            self.hparams.noise_std,
                            self.hparams.N_importance,
                            self.hparams.chunk, # chunk size is effective in val mode
                            self.train_dataset.white_back)

            for k, v in rendered_ray_chunks.items():
                results[k] += [v]

        for k, v in results.items():
            results[k] = torch.cat(v, 0)
        return results

    def setup(self, stage):
        dataset = dataset_dict[self.hparams.dataset_name]
        kwargs = {'root_dir': self.hparams.root_dir}
        if self.hparams.dataset_name == 'phototourism':
            kwargs['img_downscale'] = self.hparams.img_downscale
            kwargs['val_num'] = self.hparams.num_gpus
            kwargs['use_cache'] = self.hparams.use_cache
        elif self.hparams.dataset_name == 'blender':
            kwargs['img_wh'] = tuple(self.hparams.img_wh)
            kwargs['perturbation'] = self.hparams.data_perturb
        print("get train dataset")
        self.train_dataset = dataset(split='train', img_wh=tuple(self.hparams.img_wh), **kwargs)
        print("get val dataset")
        self.val_dataset = dataset(split='val', img_wh=tuple(self.hparams.img_wh), **kwargs)

    def configure_optimizers(self):
        self.optimizer = get_optimizer(self.hparams, self.models_to_train)
        scheduler = get_scheduler(self.hparams, self.optimizer)
        return [self.optimizer], [scheduler]

    def train_dataloader(self):
        print(f"Training Dataset Length: {len(self.train_dataset)}")
        return DataLoader(self.train_dataset,
                          shuffle=True,
                          num_workers=4,
                          batch_size=self.hparams.batch_size,
                          pin_memory=True)

    def val_dataloader(self):
        print(f"Validation Dataset Length: {len(self.val_dataset)}")
        return DataLoader(self.val_dataset,
                          shuffle=False,
                          num_workers=4,
                          batch_size=1, # validate one image (H*W rays) at a time
                          pin_memory=True)
    
    def training_step(self, batch, batch_nb):
        # print("In the Training Step")
        rays, rgbs, ts, outfit_code = batch['rays'], batch['rgbs'], batch['ts'], batch['outfit_code']
        # print("outfit_code:", outfit_code)
        # print("type of outfit_code:", type(outfit_code))
        results = self(rays, ts, outfit_code)
        loss_d = self.loss(results, rgbs)
        loss = sum(l for l in loss_d.values())

        with torch.no_grad():
            typ = 'fine' if 'rgb_fine' in results else 'coarse'
            psnr_ = psnr(results[f'rgb_{typ}'], rgbs)

        self.log('lr', get_learning_rate(self.optimizer))
        self.log('train/loss', loss)
        for k, v in loss_d.items():
            self.log(f'train/{k}', v, prog_bar=True)
        self.log('train/psnr', psnr_, prog_bar=True)

        return loss

    def validation_step(self, batch, batch_nb):
        # print("In the Validation Step")
        rays, rgbs, ts, outfit_code = batch['rays'], batch['rgbs'], batch['ts'], batch['outfit_code']
        print("rays shape in validation_step", rays.shape)
        print("rgbs shape in validation_step", rgbs.shape)
        print("outfit_code shape in validatiaon_step:", outfit_code.shape)
        # print(batch)
        rays = rays.squeeze() # (H*W, 3)
        rgbs = rgbs.squeeze() # (H*W, 3)
        ts = ts.squeeze() # (H*W)
        results = self(rays, ts, outfit_code)
        # print("shape of results['rgb_coarse'] in the validation_step:", results['rgb_coarse'].shape)
        # print("shape of rgbs in the validation_step:", rgbs.shape)
        # print("shape of results['rgb_fine'] in the validation_step:", results['rgb_fine'].shape)
        loss_d = self.loss(results, rgbs)
        loss = sum(l for l in loss_d.values())
        log = {'val_loss': loss}
        typ = 'fine' if 'rgb_fine' in results else 'coarse'

        psnr_ = psnr(results[f'rgb_{typ}'], rgbs)
        self.validation_outputs.append({'val_loss': loss, 'val_psnr': psnr_})

        if batch_nb == 0:
            W, H = self.hparams.img_wh
            img = results[f'rgb_{typ}'].view(H, W, 3).permute(2, 0, 1).cpu()  # (3, H, W)
            img_gt = rgbs.view(H, W, 3).permute(2, 0, 1).cpu()  # (3, H, W)
            depth = visualize_depth(results[f'depth_{typ}'].view(H, W))  # (3, H, W)
            stack = torch.stack([img_gt, img, depth])  # (3, 3, H, W)
            self.logger.experiment.add_images('val/GT_pred_depth', stack, self.global_step)

        return loss

    def on_validation_epoch_end(self):
        # Aggregate validation outputs
        mean_loss = torch.stack([x['val_loss'] for x in self.validation_outputs]).mean()
        mean_psnr = torch.stack([x['val_psnr'] for x in self.validation_outputs]).mean()

        # Log aggregated metrics
        self.log('val/loss', mean_loss, prog_bar=True)
        self.log('val/psnr', mean_psnr, prog_bar=True)

        # Clear the outputs for the next epoch
        self.validation_outputs.clear()


def main(hparams):
    system = NeRFSystem(hparams)
    checkpoint_callback = ModelCheckpoint(
        dirpath=os.path.join('ckpts', hparams.exp_name),  # Directory to save checkpoints
        filename='{epoch:d}',  # Format for checkpoint filenames
        monitor='val/psnr',    # Metric to monitor
        mode='max',            # Save the checkpoint with the maximum val/psnr
        save_top_k=-1          # Save all checkpoints
    )
    
    # Define logger
    logger = TensorBoardLogger(
        save_dir='logs',
        name=hparams.exp_name
    )

    trainer = Trainer(
        max_epochs=hparams.num_epochs,
        callbacks=[checkpoint_callback],
        logger=logger,
        devices=hparams.num_gpus if hparams.num_gpus > 0 else 1,  # Default to 1 device if none specified
        accelerator='gpu' if hparams.num_gpus > 0 else 'cpu',  # Choose between GPU and CPU
        strategy='ddp' if hparams.num_gpus > 1 else 'auto',  # Use 'auto' for single device
        num_sanity_val_steps=1,
    )

    print("Starting training...")
    trainer.fit(system)


if __name__ == '__main__':
    hparams = get_opts()
    print(hparams)
    main(hparams)