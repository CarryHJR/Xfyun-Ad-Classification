gpus = "0"
learning_rate = 2e-5
batch_size = 64
n_epochs = 30
model_name = "hfl/chinese-roberta-wwm-ext"
image_size = 384
fold = -1
drop_rate = 0.3
num_classes = 137
smooth = 0.1
alpha = 0
max_length = 256
long_resize = False
imbalance_sample = False
offline = True
proj = "baseline"
tag = "rbt_drop0.3_ep30_lr2e-5_bs64_mlen256"

import os
os.environ["CUDA_VISIBLE_DEVICES"] = gpus
import warnings
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
import re
import cv2
import glob
from PIL import Image
from sklearn.metrics import accuracy_score
from sklearn.model_selection import StratifiedKFold

import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader
import albumentations as A
import pytorch_lightning as pl
from pytorch_lightning.loggers import WandbLogger
import timm
import transformers
from torchsampler import ImbalancedDatasetSampler
from utils.loss.smooth import LabelSmoothingLoss
from utils.mixup import mixup_data, mixup_criterion
pl.seed_everything(0)

class Model(pl.LightningModule):
    def __init__(self, learning_rate = 1e-3, batch_size = 64, n_epochs = 30, model_name = "resnet18", image_size = 256, fold = 0, drop_rate = 0, num_classes = 137, smooth = 0, train_trans = None, valid_trans = None, criterion = None, alpha = 0, imbalance_sample = False, long_resize = False, max_length = 128):
        super(Model, self).__init__()
        self.learning_rate = learning_rate
        self.batch_size = batch_size
        self.n_epochs = n_epochs
        self.model_name = model_name
        self.image_size = image_size
        self.fold = fold
        self.drop_rate = drop_rate
        self.num_classes = num_classes
        self.smooth = smooth
        self.train_trans = train_trans
        self.valid_trans = valid_trans
        self.criterion = criterion
        self.alpha = alpha
        self.imbalance_sample = imbalance_sample
        self.long_resize = long_resize
        self.max_length = max_length
        self.save_hyperparameters()
        self.model = transformers.BertForSequenceClassification.from_pretrained(model_name)
        self.model.dropout = nn.Dropout(drop_rate)
        self.model.classifier = nn.Linear(self.model.classifier.in_features, num_classes)
        self.tokenizer = transformers.BertTokenizer.from_pretrained(model_name)

    def configure_optimizers(self):
        optimizer = torch.optim.AdamW(self.model.parameters(), lr = self.learning_rate, weight_decay = 2e-5)
        lr_scheduler = {'scheduler': torch.optim.lr_scheduler.OneCycleLR(optimizer, max_lr = self.learning_rate, steps_per_epoch = int(len(self.train_dataloader())), epochs = self.n_epochs, anneal_strategy = "linear", final_div_factor = 30,), 'name': 'learning_rate', 'interval':'step', 'frequency': 1}
        return [optimizer], [lr_scheduler]

    def forward(self, kwargs):
        out = self.model(**kwargs)["logits"]
        return out

    def training_step(self, batch, batch_idx):
        x, y = batch
        yhat = self(x)
        loss = self.criterion(yhat, y)
        self.log("train_loss", loss)
        return loss

    def validation_step(self, batch, batch_idx):
        x, y = batch
        yhat = self(x)
        loss = self.criterion(yhat, y)
        self.log("valid_loss", loss)
        return y, yhat

    def validation_step_end(self, output):
        return output

    def validation_epoch_end(self, outputs):
        y = torch.cat([_[0] for _ in outputs]).detach().cpu().numpy()
        yhat = torch.cat([_[1] for _ in outputs]).argmax(1).detach().cpu().numpy()
        acc = accuracy_score(y, yhat)
        self.log("valid_metric", acc, prog_bar = True)

    class Data(Dataset):
        def __init__(self, df, trans, max_length):
            self.df = df
            self.trans = trans
            self.max_length = max_length

        def __getitem__(self, idx):
            label = self.df.loc[idx, "label"]
            file_name = self.df.loc[idx, "file_name"]
            text = self.df.loc[idx, "text"]
            tok = self.trans.encode_plus(
                text,
                add_special_tokens=True,
                truncation = 'longest_first',
                max_length = self.max_length,
                padding="max_length")
            tok = {k: np.array(v).astype(np.long) for k, v in tok.items()}
            return tok, label

        def __len__(self):
            return len(self.df)

    def prepare_data(self):
        image_files = glob.glob("./data/train/*/*.*")
        df = pd.DataFrame(image_files, columns = ["file_name"])
        df["label"] = df.file_name.apply(lambda x: int(x.split('/')[-2]))
        split = StratifiedKFold(5, shuffle = True, random_state = 0)
        train_idx, valid_idx = list(split.split(df, df.label))[self.fold]
        df = df.merge(pd.read_csv("./data/train.tsv", sep = "\t"), on = "file_name")
        df = df.fillna("")
        df.text = df.text.apply(lambda x: re.sub(r"[^\u4e00-\u9fef]", "", x))
        df_train = df.loc[train_idx[np.isin(train_idx, df.index)]].reset_index(drop = True)
        df_valid = df.loc[valid_idx[np.isin(valid_idx, df.index)]].reset_index(drop = True)
        self.ds_train = self.Data(df_train, self.tokenizer, self.max_length)
        self.ds_valid = self.Data(df_valid, self.tokenizer, self.max_length)

    def train_dataloader(self):
        if self.imbalance_sample:
            sampler = ImbalancedDatasetSampler(self.ds_train, callback_get_label = lambda x: np.array(x.df.label))
            return DataLoader(self.ds_train, self.batch_size, sampler = sampler, num_workers = 4)
        else:
            return DataLoader(self.ds_train, self.batch_size, shuffle = True, num_workers = 4)

    def val_dataloader(self):
        return DataLoader(self.ds_valid, self.batch_size, num_workers = 4)

if __name__ == "__main__":
    train_trans = None
    valid_trans = None
    logger = WandbLogger(project = proj, name = tag, version = tag + "_" + str(fold), offline = offline)
    callback = pl.callbacks.ModelCheckpoint(
        filename = '{epoch}_{valid_metric:.3f}',
        save_last = True,
        mode = "max",
        monitor = 'valid_metric'
    )
    model = Model(
        learning_rate = learning_rate, 
        batch_size = batch_size, 
        n_epochs = n_epochs, 
        model_name = model_name, 
        image_size = image_size, 
        fold = fold, 
        drop_rate = drop_rate, 
        num_classes = num_classes, 
        smooth = smooth, 
        train_trans = train_trans, 
        valid_trans = valid_trans,
        alpha = alpha,
        imbalance_sample = imbalance_sample,
        criterion = LabelSmoothingLoss(num_classes, smooth),
        long_resize = long_resize,
        max_length = max_length
    )
    trainer = pl.Trainer(
        gpus = len(gpus.split(",")), 
        precision = 16, amp_backend = "native", amp_level = "O1", 
        accelerator = "dp",
        gradient_clip_val = 0.5,
        max_epochs = n_epochs,
        stochastic_weight_avg = True,
        logger = logger,
        progress_bar_refresh_rate = 10,
        callbacks = [callback]
    )
    trainer.fit(model)
