#! /usr/bin/env python3

import torch
import torchvision.transforms as T

from PIL import Image, ImageDraw
from typing import Optional, Union, List

from torchmetrics import ConfusionMatrix
from torchmetrics.detection.mean_ap import MeanAveragePrecision

from typing import Tuple, List, Dict



class Metrics:
    def __init__(self, num_classes=4, device='cpu'):
        self.mAP = MeanAveragePrecision(box_format="cxcywh", class_metrics=True)
        self.confusion = ConfusionMatrix(num_classes=num_classes)
        self.confusion_preds = None
        self.confusion_labels = None

        self.confusion.to(device)

    def update(self, preds, labels):
        bs, pred_shape, Sy, Sx = preds.shape
        bs, label_shape, Sy, Sx = labels.shape

        mAP_preds, mAP_labels = self.format_for_mAP(preds, labels)

        confusion_preds, confusion_labels = self.format_for_confusion(preds, labels)

        if self.confusion_preds is None:
            self.confusion_preds = confusion_preds
        else:
            self.confusion_preds = torch.vstack((self.confusion_preds, confusion_preds))

        if self.confusion_labels is None:
            self.confusion_labels = confusion_labels
        else:
            self.confusion_labels = torch.vstack((self.confusion_labels, confusion_labels))

        self.mAP.update(mAP_preds, mAP_labels)
        self.confusion.update(confusion_preds, confusion_labels)

    def compute(self):
        return (
            self.mAP.compute(),
            (self.confusion_preds, self.confusion_labels)
        )

    @staticmethod
    def format_for_confusion(
        batch_preds, batch_labels
    ) -> Tuple[List[Dict[str, torch.Tensor]], List[Dict[str, torch.Tensor]]]:
        bs, pred_shape, Sy, Sx = batch_preds.shape
        bs, label_shape, Sy, Sx = batch_labels.shape

        batch_preds[:, 5:, :, :] = torch.softmax(batch_preds[:, 5:, :, :], dim=1)
        confusion_batch_preds = (
            batch_preds.permute(1, 0, 2, 3)[5:, ...].reshape(-1, bs * Sx * Sy).T
        )
        confusion_labels = (
                batch_labels.permute(1,0,2,3)[5, :, :, :].reshape(1, bs * Sx * Sy).permute(1,0).long()
        )
        return confusion_batch_preds, confusion_labels

    @staticmethod
    def format_for_mAP(
        batch_preds, batch_labels
    ) -> Tuple[List[Dict[str, torch.Tensor]], List[Dict[str, torch.Tensor]]]:
        bs, label_shape, Sy, Sx = batch_labels.shape
        bs, pred_shape, Sy, Sx = batch_preds.shape

        device = batch_preds.device
        preds, labels = [], []
        for b, (img_preds, img_labels) in enumerate(zip(batch_preds, batch_labels)):
            if torch.all(img_labels[0, ...] == 0).item():
                # mask says there are no labels!
                labels.append(
                    {
                        "boxes": torch.tensor([], device=device),
                        "labels": torch.tensor([], device=device),
                    }
                )
                preds.append(
                    {
                        "boxes": torch.tensor([], device=device),
                        "labels": torch.tensor([], device=device),
                        "scores": torch.tensor([], device=device),
                    }
                )
            else:
                # view -> T keeps tensor as a view, and no copies?
                row_ordered_img_preds = img_preds.view(-1, Sy * Sx).T
                row_ordered_img_labels = img_labels.view(-1, Sy * Sx).T

                # if label[0] == 0, there is no box in cell Sx/Sy - mask those out
                mask = row_ordered_img_labels[..., 0] == 1

                labels.append(
                    {
                        "boxes": row_ordered_img_labels[mask, 1:5],
                        "labels": row_ordered_img_labels[mask, 5],
                    }
                )
                preds.append(
                    {
                        "boxes": row_ordered_img_preds[mask, :4],
                        "scores": row_ordered_img_preds[mask, 4],
                        "labels": torch.argmax(row_ordered_img_preds[mask, 5:], dim=1),
                    }
                )

        return preds, labels


def batch_mAP(batch_preds, batch_labels):
    formatted_batch_preds, formatted_batch_labels = format_for_mAP(
        batch_preds, batch_labels
    )
    metric = MeanAveragePrecision(box_format="cxcywh")
    metric.update(formatted_batch_preds, formatted_batch_labels)
    return metric.compute()


def draw_rects(
    img: torch.Tensor, rects: Union[torch.Tensor, List], thresh: Optional[float] = None
) -> Image:
    """
    img is the torch tensor representing an image
    rects is either
        - a torch.tensor of shape (pred, Sy, Sx), where pred = (xc, yc, w, h, confidence, ...)
        - a list of (class, xc, yc, w, h)
    thresh is a threshold for confidence when rects is a torch.Tensor
    """
    assert (
        len(img.shape) == 2
    ), f"takes single grayscale image - should be 2d, got {img.shape}"
    h, w = img.shape

    if isinstance(rects, torch.Tensor):
        pred_dim, Sy, Sx = rects.shape
        if thresh is None:
            thresh = 0.0
        rects = [r for r in rects.reshape(pred_dim, Sx * Sy).T if r[4] > thresh]
    elif isinstance(rects, list):
        if thresh is not None:
            raise ValueError("threshold only valid for tensor (i.e. prediction) input")
        rects = [r[1:] for r in rects]

    formatted_rects = [
        [
            int(w * (r[0] - r[2] / 2)),
            int(h * (r[1] - r[3] / 2)),
            int(w * (r[0] + r[2] / 2)),
            int(h * (r[1] + r[3] / 2)),
        ]
        for r in rects
    ]

    image = T.ToPILImage()(img[None, ...])
    rgb = Image.new("RGB", image.size)
    rgb.paste(image)
    draw = ImageDraw.Draw(rgb)

    for r in formatted_rects:
        draw.rectangle(r, outline="red")

    return rgb
