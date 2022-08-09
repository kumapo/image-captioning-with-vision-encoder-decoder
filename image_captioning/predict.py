import argparse
import transformers
import torch
import datasets
import evaluate
import PIL
import os
import pathlib

import numpy as np

from tqdm.notebook import tqdm
from .utils import (
    seed_everything,
    save_args
)

def main(args: argparse.Namespace):
    if not os.path.exists(args.output_dir):
        os.makedirs(args.output_dir)
    print('predict: args=%s' % args.__dict__)
    save_args(args, args.output_dir / 'predict_args.json')

    # load a fine-tuned image captioning model and corresponding tokenizer and feature extractor
    model = transformers.VisionEncoderDecoderModel.from_pretrained(args.encoder_decoder_model_name_or_path)
    tokenizer = transformers.GPT2TokenizerFast.from_pretrained(args.encoder_decoder_model_name_or_path)
    feature_extractor = transformers.ViTFeatureExtractor.from_pretrained(args.encoder_decoder_model_name_or_path)

    eval_dataset = datasets.load_dataset(
        "kumapo/coco_dataset_script", "2017",
        data_dir=str(args.valid_data_dir), split="validation", streaming=True
    )
    # https://github.com/huggingface/datasets/issues/4675
    def preprocess_function(examples):
        # prepare image (i.e. resize + normalize)
        pixel_values = feature_extractor(
            [PIL.Image.open(path).convert("RGB") for path in examples['image_path']],
            return_tensors="np"
        ).pixel_values
        # add labels (input_ids) by encoding the text
        encoded = tokenizer(
            [label for label in examples['caption']], 
            padding="max_length",
            max_length=args.max_sequence_length,
            return_tensors="np",
            return_length=True
        )
        del examples
        # important: make sure that PAD tokens are ignored by the loss function
        encoded.input_ids[encoded.input_ids == tokenizer.pad_token_id] = -100
        return {
            "pixel_values": pixel_values.squeeze(),
            "labels": encoded.input_ids,
            "length": encoded.length
        }

    eval_dataset = eval_dataset.map(
        preprocess_function,
        batched=True,
        remove_columns=["image_path","caption"]
    )
    # eval_dataloader = torch.utils.data.DataLoader(
    #     eval_dataset.take(args.num_valid_data).with_format("torch"),
    #     batch_size=args.valid_batch_size,
    #     num_workers=args.num_workers # above
    # )
    eval_dataset = eval_dataset.take(args.num_valid_data).with_format("torch")

    # https://github.com/NielsRogge/Transformers-Tutorials/blob/master/TrOCR/Fine_tune_TrOCR_on_IAM_Handwriting_Database_using_Seq2SeqTrainer.ipynb
    training_args = transformers.Seq2SeqTrainingArguments(
        predict_with_generate=True,
        per_device_eval_batch_size=args.valid_batch_size,
        fp16=False if args.debug else not args.no_fp16, 
        output_dir=args.output_dir,
        # dataloader_num_workers=args.num_workers,
        report_to="tensorboard",
        seed=args.random_seed
    )

    bleu = evaluate.load("bleu")
    meteor = evaluate.load("meteor")
    def compute_metrics(pred):
        labels_ids = pred.label_ids
        pred_ids = pred.predictions
        pred_str = tokenizer.batch_decode(pred_ids, skip_special_tokens=True)
        labels_ids[labels_ids == -100] = tokenizer.pad_token_id
        label_str = tokenizer.batch_decode(labels_ids, skip_special_tokens=True)
        metrics = {}
        try:
            metrics.update(bleu.compute(predictions=pred_str, references=label_str))
        except ZeroDivisionError as e:
            metrics.update(dict(bleu="nan"))
        try:
            metrics.update(meteor.compute(predictions=pred_str, references=label_str))
        except ZeroDivisionError as e:
            metrics.update(dict(meteor="nan"))
        return metrics

    # instantiate trainer
    trainer = transformers.Seq2SeqTrainer(
        model=model,
        tokenizer=tokenizer,
        args=training_args,
        compute_metrics=compute_metrics,
        eval_dataset=eval_dataset,
        data_collator=transformers.default_data_collator,
    )

    # https://github.com/huggingface/transformers/blob/v4.21.1/src/transformers/generation_utils.py#L845
    gen_kwargs = dict(
        do_sample=True, 
        max_length=args.max_sequence_length, 
        top_k=50, 
        top_p=0.95, 
        num_return_sequences=1
    )
    # evaluate
    metrics = trainer.evaluate(eval_dataset, **gen_kwargs)
    print("Validation metrics:", metrics)
 
    # prediction
    pred = trainer.predict(eval_dataset.take(3).with_format("torch"), **gen_kwargs)
    labels_ids = pred.label_ids
    pred_ids = pred.predictions
    pred_str = tokenizer.batch_decode(pred_ids, skip_special_tokens=True)
    pred_str = [pred for pred in pred_str]
    labels_ids[labels_ids == -100] = tokenizer.pad_token_id
    label_str = tokenizer.batch_decode(labels_ids, skip_special_tokens=True)
    print("Validation predictions:", pred_str)
    print("Validation labels:", label_str)

    return


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--valid_data_dir", default='../input/coco-2017-val/', type=pathlib.Path
    )
    parser.add_argument(
        "--output_dir", default=pathlib.Path('output'), type=pathlib.Path, help=""
    )
    parser.add_argument(
        "--encoder_decoder_model_name_or_path", default="nlpconnect/vit-gpt2-image-captioning", type=str, help=""
    )
    parser.add_argument(
        "--max_sequence_length", default=64, type=int, help=""
    )
    parser.add_argument(
        "--valid_batch_size", default=32, type=int, help=""
    )
    # parser.add_argument(
    #     "--num_workers", default=1, type=int, help=""
    # )
    parser.add_argument(
        "--no_fp16", action="store_true", help=""
    )
    parser.add_argument(
        "--random_seed", default=42, type=int, help="Random seed for determinism."
    )
    parser.add_argument(
        "--num_valid_data", default=2000, type=int, help="number of items to evaluate on dataset."
    )
    parser.add_argument(
        "--debug", action="store_true",
    )
    args = parser.parse_args()
    seed_everything(args.random_seed)
    main(args)