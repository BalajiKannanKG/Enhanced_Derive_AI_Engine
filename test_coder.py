from llama_cpp import Llama

# Downloads (~4.7 GB) on first run, then loads from local cache
llm = Llama.from_pretrained(
    repo_id="Qwen/Qwen2.5-Coder-7B-Instruct-GGUF",
    filename="qwen2.5-coder-7b-instruct-q4_k_m.gguf",
    n_ctx=4096,
    n_threads=6,  # Adjust to your physical CPU core count
    verbose=False
)

response = llm.create_chat_completion(
    messages=[
        {"role": "system", "content": "You are an expert Python programmer."},
        {"role": "user", "content": "Write a Python function to merge two sorted lists."}
    ]
)

print(response["choices"][0]["message"]["content"])
