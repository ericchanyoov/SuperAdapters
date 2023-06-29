import re
import sys
import copy
import torch
import numpy as np
import transformers

from datasets import load_dataset

from common.base import IGNORE_INDEX
from common.prompt import PROMPT_DICT

from transformers import (
    AutoModel,
    AutoTokenizer,
    GenerationConfig
)

from peft import (
    prepare_model_for_int8_training,
    PeftModel
)

from core.llm import LLM


class ChatGLMCollator:
    def __init__(self, tokenizer) -> None:
        self.tokenizer = tokenizer

    def __call__(self, features: list) -> dict:
        seq_length = max([len(feature["input_ids"]) for feature in features]) + 1
        input_ids_list, attention_mask_list, position_ids_list, labels_list = [], [], [], []
        for feature in features:
            input_ids = feature["input_ids"] + [self.tokenizer.eos_token_id] * (seq_length - len(feature["input_ids"]))
            input_ids_list.append(input_ids)

            context_length = feature["input_ids"].index(self.tokenizer.bos_token_id)
            attention_mask = np.ones((1, seq_length, seq_length))
            attention_mask = np.tril(attention_mask)
            attention_mask[:, :, :context_length] = 1
            attention_mask = np.bool_(attention_mask < 0.5)
            attention_mask_list.append(attention_mask)

            labels = feature["labels"] + [-100] * (seq_length - len(feature["labels"]))
            labels_list.append(labels)

            position_ids = [
                np.append(np.arange(context_length), np.ones([seq_length - context_length]) * (context_length - 1))]
            position_ids.append(np.append(np.zeros([context_length - 1]), np.arange(seq_length - context_length + 1)))
            position_ids_list.append(position_ids)
        return {"input_ids": torch.LongTensor(np.array(input_ids_list)),
                "labels": torch.LongTensor(np.array(labels_list)),
                "attention_mask": torch.BoolTensor(np.array(attention_mask_list)),
                "position_ids": torch.LongTensor(np.array(position_ids_list)),
                }


class ChatGLM(LLM):
    tokenizer = None

    def get_model_tokenizer(self):
        model = AutoModel.from_pretrained(
            self.base_model,
            load_in_8bit=self.load_8bit,
            torch_dtype=torch.float16,
            trust_remote_code=True,
            device_map=self.device_map
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

    def load_train_data(self):
        if self.data_path.endswith(".json") or self.data_path.endswith(".jsonl"):
            data = load_dataset("json", data_files=self.data_path)
        else:
            data = load_dataset(self.data_path)

        return data

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

    def finetune(self):
        self.auto_device()

        if not self.lora_target_modules:
            self.lora_target_modules = [
                "query_key_value"
            ]

        model, self.tokenizer = self.get_model_tokenizer()
        if self.load_8bit:
            model = prepare_model_for_int8_training(model)

        model = self.load_adapter_config(model)

        data = self.load_train_data()

        train_data, val_data = self.split_train_data(data)

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
            report_to=None
        )

        trainer = transformers.Trainer(
            model=model,
            train_dataset=train_data,
            eval_dataset=val_data,
            args=train_args,
            data_collator=ChatGLMCollator(self.tokenizer),
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

    def generate(self, instruction, input):
        self.auto_device()

        model, self.tokenizer = self.get_model_tokenizer()

        model = PeftModel.from_pretrained(
            model,
            self.adapter_weights,
        )

        if not self.load_8bit:
            model.half()

        model.to(self.device).eval()
        if torch.__version__ >= "2" and sys.platform != "win32":
            model = torch.compile(model)

        response = self.evaluate(model, instruction, input)
        if response[-4:] == "</s>":
            response = response[:-4]

        return response


if __name__ == "__main__":
    chatglm = ChatGLM()
    chatglm.finetune()
