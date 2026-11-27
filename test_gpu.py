import openvino_genai as ov_genai

# Replace with an OpenVINO-ready model repo or local OpenVINO IR directory
model_path = "OpenVINO/Qwen2.5-Coder-7B-Instruct-int4-ov"

pipe = ov_genai.LLMPipeline(model_path, device="GPU")
output = pipe.generate("Write a short Python function to reverse a list.", max_new_tokens=100)
print(output)