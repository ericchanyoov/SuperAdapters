import os
import sys
import re
import copy
import torch
import transformers
from typing import Dict, Optional, Sequence, Union

from common.base import IGNORE_INDEX
from common.prompt import PROMPT_DICT

from transformers import (
    AutoModel,
    AutoTokenizer,
    GenerationConfig,
    DataCollatorWithPadding,
    BatchEncoding,
    BitsAndBytesConfig
)
from transformers.modeling_utils import PreTrainedModel
from transformers.tokenization_utils import PreTrainedTokenizer

from peft import (
    prepare_model_for_int8_training,
    set_peft_model_state_dict,
    PeftModel
)

from core.llm import LLM


class ChatGLMCollator(DataCollatorWithPadding):
    r"""
    Data collator for ChatGLM. It is capable of dynamically padding for batched data.
    """

    def __init__(
            self,
            tokenizer: PreTrainedTokenizer,
            model: PreTrainedModel,
            ignore_pad_token_for_loss: Optional[bool] = False,
            use_v2: Optional[bool] = False
    ):
        super().__init__(tokenizer, padding=True)
        self.model = model
        self.label_pad_token_id = IGNORE_INDEX if ignore_pad_token_for_loss else tokenizer.pad_token_id
        if use_v2:
            self.get_attention_masks = self.get_attention_masks_v2
            self.get_position_ids = self.get_position_ids_v2
        else:
            self.get_attention_masks = self.get_attention_masks_v1
            self.get_position_ids = self.get_position_ids_v1

    def get_attention_masks_v1(self, input_ids: torch.Tensor, device: torch.device) -> torch.Tensor:
        r"""
        Generates attention masks for left-padded sequences.

        Note that ChatGLM assigns False on token to be attended in attention mask. In general settings, it should be True.

        According to: https://huggingface.co/THUDM/chatglm-6b/blob/v1.1.0/modeling_chatglm.py#L680
        """
        batch_size, seq_length = input_ids.size()
        attention_mask = torch.ones((batch_size, seq_length, seq_length), device=device)
        attention_mask.tril_()

        for i, seq in enumerate(input_ids):
            attention_mask[i, :, :(seq == self.tokenizer.bos_token_id).nonzero()[0].item()] = 1  # context
            attention_mask[i, :, :(seq != self.tokenizer.pad_token_id).nonzero()[0].item()] = 0  # padding

        attention_mask.unsqueeze_(1)
        attention_mask = (attention_mask < 0.5).bool()
        return attention_mask

    def get_position_ids_v1(self, input_ids: torch.Tensor, device: torch.device) -> torch.Tensor:
        r"""
        Generates position ids for left-padded sequenes.

        According to: https://huggingface.co/THUDM/chatglm-6b/blob/v1.1.0/modeling_chatglm.py#L692
        """
        batch_size, seq_length = input_ids.size()
        mask: int = self.model.config.mask_token_id
        gmask: int = self.model.config.gmask_token_id
        position_ids = torch.zeros((batch_size, seq_length), dtype=torch.long, device=device)
        block_position_ids = torch.zeros((batch_size, seq_length), dtype=torch.long, device=device)

        for i, seq in enumerate(input_ids):
            mask_token = gmask if gmask in seq else mask
            context_length = (seq == self.tokenizer.bos_token_id).nonzero()[0].item()
            padding_length = (seq != self.tokenizer.pad_token_id).nonzero()[0].item()
            position_ids[i, padding_length:] = torch.arange(
                seq_length - padding_length,
                dtype=torch.long,
                device=device
            )
            if self.model.position_encoding_2d or (mask_token != gmask):  # 2d position encoding or not gMASK
                position_ids[i, context_length:] = (seq == mask_token).nonzero()[
                                                       0].item() - padding_length  # mask position
            block_position_ids[i, context_length:] = torch.arange(
                seq_length - context_length,
                dtype=torch.long,
                device=device
            ) + 1

        if self.model.position_encoding_2d:
            position_ids = torch.stack((position_ids, block_position_ids), dim=1)

        return position_ids

    def get_attention_masks_v2(self, input_ids: torch.Tensor, device: torch.device) -> torch.Tensor:
        r"""
        Generates attention masks for left-padded sequences.
        """
        batch_size, seq_length = input_ids.size()
        attention_mask = torch.ones((batch_size, seq_length), device=device)

        for i, seq in enumerate(input_ids):
            attention_mask[i, :(seq != self.tokenizer.pad_token_id).nonzero()[0].item()] = 0  # padding

        return attention_mask

    def get_position_ids_v2(self, input_ids: torch.Tensor, device: torch.device) -> torch.Tensor:
        r"""
        Generates position ids for left-padded sequenes.
        """
        batch_size, seq_length = input_ids.size()
        position_ids = torch.zeros((batch_size, seq_length), dtype=torch.long, device=device)

        for i, seq in enumerate(input_ids):
            padding_length = (seq != self.tokenizer.pad_token_id).nonzero()[0].item()
            position_ids[i, padding_length:] = torch.arange(seq_length - padding_length, dtype=torch.long,
                                                            device=device)

        return position_ids

    def __call__(self, features: Sequence[Dict[str, Union[torch.Tensor, Sequence[int]]]]) -> BatchEncoding:
        r"""
        Pads batched data to the longest sequence in the batch.

        We adopt left-padding in both training and evaluation.
        """
        if isinstance(features[0]["input_ids"], torch.Tensor):
            input_ids = [feature["input_ids"].clone().detach().flip(0) for feature in features]
        else:
            input_ids = [torch.tensor(feature["input_ids"]).flip(0) for feature in features]

        if "labels" in features[0]:
            if isinstance(features[0]["labels"], torch.Tensor):
                labels = [feature["labels"].clone().detach().flip(0) for feature in features]
            else:
                labels = [torch.tensor(feature["labels"]).flip(0) for feature in features]
            input_ids = input_ids + labels  # pad them to the same length

        input_ids = torch.nn.utils.rnn.pad_sequence(
            input_ids,
            batch_first=True,
            padding_value=self.tokenizer.pad_token_id
        ).flip(-1)

        batch = {}

        if "labels" in features[0]:
            input_ids, labels = input_ids.split(len(features), dim=0)
            labels = torch.where(labels != self.tokenizer.pad_token_id, labels, self.label_pad_token_id)
            batch["labels"] = labels

        batch["input_ids"] = input_ids
        batch["attention_mask"] = self.get_attention_masks(input_ids, device=input_ids.device)
        batch["position_ids"] = self.get_position_ids(input_ids, device=input_ids.device)

        return BatchEncoding(batch)


class ChatGLMSeq2Seq(LLM):
    tokenizer = None

    def get_model_tokenizer(self):
        bnb_config = None
        if self.adapter == "qlora":
            bnb_config = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=torch.float16
            )
        model = AutoModel.from_pretrained(
            self.base_model,
            load_in_8bit=self.load_8bit,
            torch_dtype=torch.float16,
            trust_remote_code=True,
            device_map=self.device_map,
            quantization_config=bnb_config
        )
        tokenizer = AutoTokenizer.from_pretrained(
            self.base_model,
            trust_remote_code=True,
            add_eos_token=self.add_eos_token
        )  # default add_eos_token=False

        return model, tokenizer

    def generate_prompt(self, data_point):
        # a nasty solution just for now
        if 'Human:' in data_point["instruction"] and 'Assistant:' in data_point["instruction"]:  # TODO
            data_point["instruction"] = data_point["instruction"].replace('Human:', '### Human: ')
            data_point["instruction"] = data_point["instruction"].replace('Assistant:', '### Assistant: ')

            return PROMPT_DICT['prompt_multirun_input'].format_map(data_point)

        prompt_ = PROMPT_DICT['prompt_input'] if data_point["input"] else PROMPT_DICT['prompt_no_input']

        return prompt_.format_map(data_point)

    def prompt_tokenize(self, prompt):
        input_ids = self.tokenizer.encode(
            prompt,
            truncation=True,
            max_length=self.cutoff_len,
            #    padding="max_length",
            padding=False,
        )
        return {
            "input_ids": input_ids,
            "labels": copy.deepcopy(input_ids)
        }

    def completion_tokenize(self, completion):
        input_ids = self.tokenizer.encode(
            completion,
            truncation=True,
            max_length=self.cutoff_len,
            # add_special_tokens=False
        )
        return {
            "input_ids": input_ids,
            "labels": copy.deepcopy(input_ids)
        }

    def tokenize_prompt(self, data_point):
        prompt_no_resp = self.generate_prompt(data_point)

        if 'multi-round dialogue' in prompt_no_resp:
            inputs_with_offsets = self.tokenizer(prompt_no_resp, return_offsets_mapping=True)
            labels = copy.deepcopy(inputs_with_offsets['input_ids'])
            source_len = len(
                self.tokenizer(PROMPT_DICT['prompt_multirun_input'].split('\n\n')[0] + '\n\n')['input_ids'])
            labels[:source_len] = [IGNORE_INDEX] * source_len
            offsets = inputs_with_offsets["offset_mapping"]

            matches = re.finditer(r'### (?!Assistant:)(.*?)</s>', prompt_no_resp, re.DOTALL)

            for match in matches:
                start_pos, end_pos = match.span()
                start_idx = None
                end_idx = None

                for i, (start, end) in enumerate(offsets):
                    if start <= start_pos < end:
                        start_idx = i
                    if start <= end_pos < end:
                        end_idx = i

                if start_idx is not None and end_idx is not None:
                    for i in range(start_idx, end_idx - 1):
                        labels[i] = IGNORE_INDEX

            return dict(
                input_ids=inputs_with_offsets['input_ids'],
                attention_mask=inputs_with_offsets['attention_mask'],
                labels=labels,
            )
        else:
            tokenized_result = self.prompt_tokenize(prompt_no_resp)

            source_len = len(tokenized_result['input_ids'])
            prompt_with_response = prompt_no_resp + " " + data_point["output"]
            prompt_with_response += " " + self.tokenizer.eos_token

            tokenized_with_response = self.completion_tokenize(prompt_with_response)
            tokenized_with_response["input_ids"] = tokenized_result['input_ids'] + tokenized_with_response["input_ids"][
                                                                                   source_len - 2:-2]
            tokenized_with_response["labels"] = tokenized_result['labels'] + tokenized_with_response["labels"][
                                                                             source_len - 2:-2]

            tokenized_with_response["labels"] = [IGNORE_INDEX] * source_len + tokenized_with_response["labels"][
                                                                              source_len:]

            return tokenized_with_response

    def split_train_data(self, data):
        if self.val_set_size > 0:
            train_val = data["train"].train_test_split(
                test_size=self.val_set_size, shuffle=True, seed=42
            )
            train_data = (
                train_val["train"].shuffle().map(self.tokenize_prompt)
            )
            val_data = (
                train_val["test"].shuffle().map(self.tokenize_prompt)
            )
        else:
            train_data = data["train"].shuffle().map(self.tokenize_prompt)
            val_data = None

        return train_data, val_data

    def finetune(self, fromdb, iteration):
        self.auto_device()

        if not self.lora_target_modules:
            self.lora_target_modules = [
                "query_key_value"
            ]

        model, self.tokenizer = self.get_model_tokenizer()
        if self.load_8bit:
            model = prepare_model_for_int8_training(model)

        model = self.load_adapter_config(model)

        data = self.load_train_data(fromdb, iteration)
        print(data)
        if not data:
            print("Warning! Empty Train Data!")
            return

        train_data, val_data = self.split_train_data(data)

        if self.resume_from_checkpoint:
            # Check the available weights and load them
            checkpoint_name = os.path.join(
                self.resume_from_checkpoint, "pytorch_model.bin"
            )  # Full checkpoint
            if not os.path.exists(checkpoint_name):
                checkpoint_name = os.path.join(
                    self.resume_from_checkpoint, "adapter_model.bin"
                )  # only LoRA model - LoRA config above has to fit
                self.resume_from_checkpoint = (
                    False  # So the trainer won't try loading its state
                )
            # The two files above have a different name depending on how they were saved, but are actually the same.
            if os.path.exists(checkpoint_name):
                print(f"Restarting from {checkpoint_name}")
                adapters_weights = torch.load(checkpoint_name)
                set_peft_model_state_dict(model, adapters_weights)
            else:
                print(f"Checkpoint {checkpoint_name} not found")

        total_batch_size = self.per_gpu_train_batch_size * self.gradient_accumulation_steps * (
            self.world_size if self.ddp else 1)
        total_optim_steps = train_data.num_rows // total_batch_size
        saving_step = int(total_optim_steps / 10)
        warmup_steps = int(total_optim_steps / 10)

        train_args = transformers.TrainingArguments(
            per_device_train_batch_size=self.per_gpu_train_batch_size,
            gradient_accumulation_steps=self.gradient_accumulation_steps,
            warmup_steps=warmup_steps,
            num_train_epochs=self.epochs,
            learning_rate=self.learning_rate,
            fp16=self.is_fp16,
            optim="adamw_torch",
            logging_steps=self.logging_steps,
            evaluation_strategy="steps" if self.val_set_size > 0 else "no",
            save_strategy="steps",
            eval_steps=saving_step if self.val_set_size > 0 else None,
            save_steps=saving_step,
            # max_steps=200,
            output_dir=self.output_dir,
            save_total_limit=11,
            load_best_model_at_end=True if self.val_set_size > 0 else False,
            ddp_find_unused_parameters=False if self.ddp else None,
            group_by_length=self.group_by_length,
            use_mps_device=self.use_mps_device,
            report_to=None if self.disable_wandb else "wandb"
        )

        trainer = transformers.Trainer(
            model=model,
            train_dataset=train_data,
            eval_dataset=val_data,
            args=train_args,
            data_collator=ChatGLMCollator(
                self.tokenizer,
                model=model,
                ignore_pad_token_for_loss=False,
                use_v2=True if self.model_type == "chatglm2" else False
            ),
        )

        model.config.use_cache = False

        if torch.__version__ >= "2" and sys.platform != "win32":
            model = torch.compile(model)

        trainer.train(resume_from_checkpoint=self.resume_from_checkpoint)

        model.save_pretrained(self.output_dir)

        print("\n If there's a warning about missing keys above, please disregard :)")

    def generate_eval_prompt(self, instruction, input=None):
        if input:
            return f"""Below is an instruction that describes a task, paired with an input that provides further context. Write a response that appropriately completes the request.

    ### Instruction:
    {instruction}

    ### Input:
    {input}

    ### Response:"""
        else:
            return f"""Below is an instruction that describes a task. Write a response that appropriately completes the request.

    ### Instruction:
    {instruction}

    ### Response:"""

    def evaluate(self,
                 model,
                 instruction,
                 input=None,
                 **kwargs,
                 ):
        prompt = self.generate_eval_prompt(instruction, input)
        inputs = self.tokenizer(prompt, return_tensors="pt")
        input_ids = inputs["input_ids"].to(self.device)
        generation_config = GenerationConfig(
            temperature=self.temperature,
            top_p=self.top_p,
            top_k=self.top_k,
            num_beams=4,
            do_sample=True,
            no_repeat_ngram_size=6,
            repetition_penalty=1.8,
            **kwargs,
        )
        with torch.no_grad():
            generation_output = model.generate(
                input_ids=input_ids,
                generation_config=generation_config,
                return_dict_in_generate=True,
                output_scores=True,
                max_new_tokens=self.max_new_tokens,
            )
        s = generation_output.sequences[0]
        output = self.tokenizer.decode(s)

        return output.split("### Response:")[1].strip()

    def generate(self, instruction, input, data, fromdb, type, iteration, test_iteration):
        self.auto_device()

        model, self.tokenizer = self.get_model_tokenizer()

        if self.adapter_weights != "None":
            model = PeftModel.from_pretrained(
                model,
                self.adapter_weights,
            )

        if not self.load_8bit:
            model.half()

        model.to(self.device).eval()
        if torch.__version__ >= "2" and sys.platform != "win32":
            model = torch.compile(model)

        eval_inputs = self.get_eval_input(instruction, input, data, fromdb, type, iteration)

        for item in eval_inputs:
            try:
                response = self.evaluate(model, item["instruction"], item["input"])
                if response[-4:] == "</s>":
                    response = response[:-4]
            except:
                response = "Eval Error"

            item["ac_output"] = response

        self.eval_output(eval_inputs, data, fromdb, type, iteration, test_iteration)


if __name__ == "__main__":
    chatglm = ChatGLMSeq2Seq()
    chatglm.finetune()
