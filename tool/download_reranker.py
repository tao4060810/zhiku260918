# 下载精排模型 bge-reranker-v2-m3 到 modelscope 缓存目录
#
# 缓存布局（modelscope 1.x）：{cache_dir}/models/{owner}--{name}/snapshots/{revision}/
# 与 tool/download_bgem3.py 保持完全一致，所以 .env 里可以用同样的写法引用。
#
# 想直接下到某个固定目录（不走缓存布局）时，把下面这行换上：
# model_dir = snapshot_download(MODEL_ID, local_dir='D:/ai_models/bge-reranker-v2-m3')

from modelscope.hub.snapshot_download import snapshot_download

# 模型仓库 ID
MODEL_ID = 'BAAI/bge-reranker-v2-m3'

# 缓存根目录（与 bge-m3 相同，下载完在 models/BAAI--bge-reranker-v2-m3/snapshots/master）
CACHE_DIR = 'D:/ai_models/modelscope_cache'

model_dir = snapshot_download(MODEL_ID, cache_dir=CACHE_DIR)
print(f"模型已下载到: {model_dir}")

# 直接把下面这行填进 .env，替换原来的 BGE_RERANKER_LARGE
print()
print("把下面这行填进 .env：")
print(f"BGE_RERANKER_V2_M3={model_dir}")