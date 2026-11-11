# Third-party notices

wissensdatenbank-mcp itself is MIT-licensed. The container image bundles the following components; their licences apply to them.

## Python packages

| Component | Licence |
|---|---|
| [LanceDB](https://github.com/lancedb/lancedb) / Lance | Apache-2.0 |
| [PyArrow](https://arrow.apache.org/) | Apache-2.0 |
| [PyTorch](https://pytorch.org/) (CPU build) | BSD-3-Clause |
| [sentence-transformers](https://github.com/UKPLab/sentence-transformers) | Apache-2.0 |
| [Transformers](https://github.com/huggingface/transformers) | Apache-2.0 |
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) | MIT |
| [Starlette](https://www.starlette.io/) | BSD-3-Clause |
| [Uvicorn](https://www.uvicorn.org/) | BSD-3-Clause |
| [AnyIO](https://github.com/agronholm/anyio) | MIT |
| [NumPy](https://numpy.org/) | BSD-3-Clause |
| [PyYAML](https://pyyaml.org/) | MIT |
| [SentencePiece](https://github.com/google/sentencepiece) | Apache-2.0 |
| [protobuf](https://github.com/protocolbuffers/protobuf) | BSD-3-Clause |
| [tiktoken](https://github.com/openai/tiktoken) | MIT |

Base image: `python:3.12-slim` (Debian; Python under the PSF licence, Debian packages under their respective licences), plus `git`, `openssh-client`, `tini` (MIT).

## Models

Models are **not** part of the image; they are downloaded from Hugging Face on first start into the `/models` volume.

| Model | Default use | Licence |
|---|---|---|
| [BAAI/bge-m3](https://huggingface.co/BAAI/bge-m3) | embeddings | MIT |
| [cross-encoder/mmarco-mMiniLMv2-L12-H384-v1](https://huggingface.co/cross-encoder/mmarco-mMiniLMv2-L12-H384-v1) | reranker | Apache-2.0 (model card); **trained on mMARCO / MS MARCO** |
| [BAAI/bge-reranker-v2-m3](https://huggingface.co/BAAI/bge-reranker-v2-m3) | optional reranker | Apache-2.0 |

**Note on the default reranker:** the model weights are published under Apache-2.0, but the training data (MS MARCO) is licensed for non-commercial research purposes only. Whether this affects the use of models trained on it is legally unsettled. For commercial deployments consider `RERANK_MODEL=BAAI/bge-reranker-v2-m3` (slower) or have the question assessed. This is not legal advice.
