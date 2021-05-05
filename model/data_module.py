import hashlib
import json
import os
import requests

from tqdm.contrib.concurrent import process_map

import torch
from torch.utils.data import DataLoader  # , TensorDataset

from transformers import PreTrainedTokenizer

import pytorch_lightning as pl


class CoQA(pl.LightningDataModule):
    class CoQADataset(torch.utils.data.Dataset):
        def __init__(self, src=None, tgt=None, vocab_size=None, filename=None):
            if filename is not None:
                self.uncompressed_type = self.compressed_type = None
                self.input_ids = self.labels = None
                self.load(filename)
            else:
                self.uncompressed_type = src.type()

                if vocab_size is not None:
                    self.compressed_type = get_int_type(vocab_size)
                else:
                    self.compressed_type = self.uncompressed_type

                self.input_ids = src.type(self.compressed_type)
                self.labels = tgt

        def __getitem__(self, idx):
            item = {"input_ids": self.input_ids[idx], "labels": self.labels[idx]}
            return item

        def __len__(self):
            return len(self.input_ids)

        def load(self, filename):
            self.uncompressed_type, self.input_ids, self.labels = torch.load(filename)
            self.compressed_type = self.input_ids.type()
            self.labels = self.labels.type(self.uncompressed_type)

        def save(self, filename):
            torch.save(
                (
                    self.uncompressed_type,
                    self.input_ids,
                    self.labels.type(self.compressed_type),
                ),
                filename,
            )

    def __init__(self, config: dict, tokenizer: PreTrainedTokenizer):
        super().__init__()
        self.config = config
        self.tokenizer = tokenizer

    def prepare_data(self):
        datasets = {
            "validate": [
                self.config["DataModule"]["val_dataset"],
                "https://nlp.stanford.edu/data/coqa/coqa-dev-v1.0.json",
            ],
            "train": [
                self.config["DataModule"]["train_dataset"],
                "https://nlp.stanford.edu/data/coqa/coqa-train-v1.0.json",
            ],
        }

        for mode, (dataset_path, dataset_url) in datasets.items():
            if not os.path.isfile(dataset_path):
                print(f"File {dataset_path} not found. Downloading from {dataset_url}")
                download_from_url(dataset_url, dataset_path)

            tokenized_path = self.get_tokenized_path(dataset_path)

            if os.path.isfile(tokenized_path):
                print(f"Found {dataset_path} tokenized. Loading from {tokenized_path}")
                dataset = self.CoQADataset(filename=tokenized_path)
            else:
                print(f"Tokenizing {dataset_path}. This might take a while...")

                dataset = self.process_data(dataset_path)
                tokenized = {}

                tokenized["src"] = self.tokenizer(
                    dataset["src"],
                    padding="longest",
                    truncation=True,
                    max_length=int(self.config["Model"]["max_input_length"]),
                    return_tensors="pt",
                ).input_ids

                print(f"Source: {len(tokenized['src'])} samples")

                tokenized["tgt"] = self.tokenizer(
                    dataset["tgt"],
                    padding="longest",
                    truncation=True,
                    max_length=int(self.config["Model"]["max_output_length"]),
                    return_tensors="pt",
                ).input_ids

                print(f"Target: {len(tokenized['tgt'])} samples")

                dataset = self.CoQADataset(
                    tokenized["src"],
                    tokenized["tgt"],
                    vocab_size=self.tokenizer.vocab_size,
                )

                dataset.save(tokenized_path)
                print(f"Saved tokenized dataset to {tokenized_path}")

            if mode == "train":
                self.train_dataset = dataset
            elif mode == "validate":
                self.val_dataset = dataset
            else:
                print("Unrecognized mode. Only supports train and validate.")

    def train_dataloader(self):
        return DataLoader(
            self.train_dataset,
            batch_size=int(self.config["DataModule"]["batch_size"]),
            shuffle=True,
            num_workers=os.cpu_count(),
            pin_memory=bool(torch.cuda.device_count()),
        )

    def val_dataloader(self):
        return DataLoader(
            self.val_dataset,
            batch_size=int(self.config["DataModule"]["batch_size"]),
            shuffle=False,
            num_workers=os.cpu_count(),
            pin_memory=bool(torch.cuda.device_count()),
        )

    def test_dataloader(self):
        return self.val_dataloader()

    @staticmethod
    def get_tokenized_path(filename):
        hash_value = hash_file(filename)[:8]
        tokenized_path = (
            os.path.splitext(filename)[0] + "_tokenized_" + hash_value + ".pt"
        )
        return tokenized_path

    @staticmethod
    def process_data(dataset_path):
        separator = "\n"

        with open(dataset_path, "r") as f:
            data = json.load(f)

        dataset = {"src": [], "tgt": []}

        for sample in data["data"]:
            context = sample["story"]

            questions = sample["questions"]
            answers = sample["answers"]

            for question, answer in zip(questions, answers):
                if question["turn_id"] != answer["turn_id"]:
                    print(
                        "question and answer turn ids don't match for sample",
                        sample["id"],
                    )

                question = question["input_text"]
                answer = answer["input_text"]

                dataset["src"].append(question + separator + context)
                dataset["tgt"].append(answer)

        return dataset


def download_from_url(url, filename):
    r = requests.get(url, allow_redirects=True)
    with open(filename, "wb") as f:
        f.write(r.content)
    return


# From https://www.geeksforgeeks.org/compare-two-files-using-hashing-in-python/
def hash_file(filename):
    # A arbitrary (but fixed) buffer
    # size (change accordingly)
    # 65536 = 65536 bytes = 64 kilobytes
    BUF_SIZE = 65536

    # Initializing the sha256() method
    sha256 = hashlib.sha256()

    # Opening the file provided as
    # the first commandline arguement
    with open(filename, "rb") as f:
        while True:
            # reading data = BUF_SIZE from
            # the file and saving it in a
            # variable
            data = f.read(BUF_SIZE)

            # True if eof = 1
            if not data:
                break

            # Passing that data to that sh256 hash
            # function (updating the function with
            # that data)
            sha256.update(data)

    # sha256.hexdigest() hashes all the input
    # data passed to the sha256() via sha256.update()
    # Acts as a finalize method, after which
    # all the input data gets hashed hexdigest()
    # hashes the data, and returns the output
    # in hexadecimal format
    return sha256.hexdigest()


def get_int_type(vocab_size):
    if vocab_size <= 2 ** 8:
        return "torch.uint8"
    else:
        for int_size, int_type in {
            16: torch.int16,
            32: torch.int32,
            64: torch.int64,
        }.items():
            if vocab_size < 2 ** (int_size - 1):
                return int_type
        return torch.int64