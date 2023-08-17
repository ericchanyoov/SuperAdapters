export LLM_DB_HOST='127.0.0.1'
export LLM_DB_PORT=3306
export LLM_DB_USERNAME='YOURUSERNAME'
export LLM_DB_PASSWORD='YOURPASSWORD'
export LLM_DB_NAME='YOURDBNAME'

python finetune.py --model_type llama --fromdb --db_iteration train --model_path "LLMs/open-llama/open-llama-3b/" --adapter "lora" --output_dir "output/llama" --disable_wandb
