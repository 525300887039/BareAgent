# Research: Local (offline) text-embedding libraries for BareAgent `[embeddings]` extra

- **Query**: Best local/offline text-embedding library for a single-user Python 3.12 CLI (BareAgent), as an optional `[embeddings]` extra. Use case: embed a few dozen short markdown memory files + a query, cosine similarity for semantic recall. Tiny working set, personal laptop (Windows + others), must work offline after first model download.
- **Scope**: external (PyPI metadata, package docs) + internal (pyproject.toml, recall() integration point)
- **Date**: 2026-06-08

All versions, declared dependencies, and wheel sizes below were pulled live from the **PyPI JSON API** during this research (not from memory). Web search MCP tools were unavailable in this environment; PyPI's JSON API was used as the authoritative source for current versions/deps/wheel sizes.

---

## Internal integration point (where this lands)

| File | Note |
|---|---|
| `src/memory/persistent.py:325` | `MemoryManager.recall(query, k)` — current MVP is **lexical** (ASCII word + CJK bigram overlap on `name + description` frontmatter). Docstring (lines 331-333) explicitly says: *"a future vector backend would replace this method body without touching the tool surface."* This is the drop-in point. |
| `src/memory/persistent.py:368` | `recall_section()` renders results; unchanged regardless of scoring backend. |
| `pyproject.toml:18-31` | Existing optional extras pattern: `lsp`, `pdf` are single-line extras with graceful lazy-import degradation. `[embeddings]` should follow the same shape. |
| `pyproject.toml:10-16` | **Core deps today: anthropic, openai, httpx, prompt-toolkit, rich. NO numpy anywhere in the project.** So numpy is NOT already present — whichever lib we pick must bring it (all three candidates below do, transitively). |

Cosine similarity over a few-dozen-vector corpus needs numpy (or could be hand-rolled in pure Python, but numpy is pulled transitively by all candidates anyway).

---

## Findings

### Candidate 1 — fastembed (Qdrant)

- **Current version**: `0.8.0` · **License**: Apache-2.0 · **requires-python**: `>=3.10`
- **Backend**: ONNX Runtime (CPU). **Does NOT pull torch.**
- **Declared deps** (from PyPI `requires_dist`):
  `huggingface-hub>=0.20`, `loguru`, `mmh3`, **`numpy>=1.26` (py3.12)**, **`onnxruntime>=1.17` (py3.12, with a few excluded versions)**, `pillow>=10.3`, `py-rust-stemmers`, `requests>=2.31`, `tokenizers>=0.15`, `tqdm`.
- **Dependency weight (wheel download sizes, live from PyPI)**:
  | Package | Windows (win_amd64, cp312) | Linux (manylinux x86_64, cp312) |
  |---|---|---|
  | fastembed | 0.1 MB | 0.1 MB |
  | onnxruntime 1.26.0 | **13.0 MB** | **18.2 MB** |
  | numpy 2.4.6 | 12.3 MB | 16.6 MB |
  | tokenizers 0.23.1 | 2.8 MB | 3.3 MB |
  | huggingface-hub 1.18.0 | 0.7 MB | 0.7 MB |
  | pillow, requests, loguru, mmh3, py-rust-stemmers, tqdm | a few MB combined | a few MB combined |

  **Total install footprint ≈ 35-45 MB** (dominated by onnxruntime + numpy). No torch.
- **Default model**: `BAAI/bge-small-en-v1.5` (the `TextEmbedding()` default). **384-dimensional** output. Quantized ONNX model download is **~80-130 MB** on first use.
- **API shape** (matches the `embed(list[str]) -> vectors` need almost exactly):
  ```python
  from fastembed import TextEmbedding
  model = TextEmbedding()                       # default BAAI/bge-small-en-v1.5
  embeddings = list(model.embed(["doc one", "doc two", "query"]))
  # -> list of numpy float32 arrays, each len 384
  ```
  `embed()` returns a **generator** of numpy arrays (must wrap in `list(...)`). numpy is already a dep, so cosine is free.
- **Offline behavior**: First use downloads the ONNX model from HuggingFace Hub into the HF cache (`~/.cache/huggingface/` on Linux/mac, `%USERPROFILE%\.cache\huggingface\` or `%HF_HOME%` on Windows). After that it runs fully offline. Honors `HF_HUB_OFFLINE=1`. Can also point `cache_dir=` at a custom path in `TextEmbedding(cache_dir=...)`.
- **Windows support**: Good. onnxruntime ships native cp312 win_amd64 wheels (13 MB). No compiler needed.
- **Latency ballpark**: ONNX CPU on a small MiniLM/BGE-small model: low-single-digit ms per short string after warm-up; a few-dozen-doc corpus embeds in well under a second. Cold start = one-time model download + ~1-2 s ONNX session init.

### Candidate 2 — sentence-transformers

- **Current version**: `5.5.1` · **License**: Apache-2.0 · **requires-python**: `>=3.10`
- **Backend**: PyTorch. **Pulls torch (heavy).**
- **Declared deps** (from PyPI `requires_dist`):
  **`torch>=1.11`**, **`transformers>=4.41`**, `huggingface-hub>=0.23`, `numpy>=1.20`, **`scikit-learn>=0.22`**, **`scipy>=1.0`**, `typing_extensions`, `tqdm`.
- **Dependency weight (wheel download sizes, live from PyPI)**:
  | Package | Windows (win_amd64, cp312) | Linux (manylinux x86_64, cp312) |
  |---|---|---|
  | sentence-transformers | 0.6 MB | 0.6 MB |
  | **torch 2.12.0** | **123.0 MB** | **532.3 MB** |
  | transformers | 11 MB | 11 MB |
  | numpy 2.4.6 | 12.3 MB | 16.6 MB |
  | scikit-learn + scipy | tens of MB | tens of MB |
  | tokenizers (via transformers) | 2.8 MB | 3.3 MB |

  **Total install footprint ≈ 200 MB on Windows, 700 MB+ on Linux** (torch is the elephant; Linux torch bundles CUDA-related extras even for the default wheel). This is **5-15x heavier** than fastembed for the identical 384-dim output.
- **Default model**: `all-MiniLM-L6-v2`, **384-dimensional**. Model download **~90 MB** on first use.
- **API shape** (the most ergonomic of the three):
  ```python
  from sentence_transformers import SentenceTransformer
  model = SentenceTransformer("all-MiniLM-L6-v2")
  embeddings = model.encode(["doc one", "doc two", "query"])   # numpy 2-D array (N, 384)
  ```
  `encode()` returns a numpy 2-D array directly; supports `normalize_embeddings=True` so cosine becomes a dot product.
- **Offline behavior**: First use downloads from HF Hub into the HF cache; honors `HF_HUB_OFFLINE=1` and `SENTENCE_TRANSFORMERS_HOME`. Fully offline after download.
- **Windows support**: Works; torch ships native win_amd64 cp312 wheels (123 MB). Heavier but reliable.
- **Latency ballpark**: torch CPU on MiniLM-L6: a few ms per short string after warm-up. **Cold start is the pain point** — importing torch + transformers is slow (often 2-5 s just to import, before any model load) and adds ~200 MB-700 MB to install. For a tiny corpus this overhead dominates and buys nothing over fastembed (same model family, same 384 dims, comparable quality).

### Candidate 3 (lighter alternative) — model2vec / static embeddings

- **Current version**: `0.8.2` · **License**: MIT · **requires-python**: `>=3.10`
- **Backend**: **static token embeddings** (no neural forward pass at inference — it's essentially a distilled lookup table + pooling). torch/onnx are only needed for *distillation/training* (`[distill]`, `[onnx]` extras), **NOT for inference**.
- **Declared deps (inference path)** (from PyPI `requires_dist`):
  `jinja2`, `joblib`, **`numpy`**, `safetensors`, `tokenizers>=0.20`, `tqdm`. **No torch, no onnxruntime, no transformers.**
- **Dependency weight**: **lightest of all three.** model2vec wheel 0.1 MB + numpy (~12 MB win) + tokenizers (2.8 MB win) + safetensors/joblib/jinja2 (a few MB). **Total ≈ 15-20 MB.**
- **Default model**: family is `minishlab/potion-base-*` (e.g. `potion-base-8M`, `potion-base-32M`). Model files are small (single-digit to low-tens of MB). Dimensions vary by model (commonly 256/512). There is no implicit default — you pass the model name to `from_pretrained`.
- **API shape**:
  ```python
  from model2vec import StaticModel
  model = StaticModel.from_pretrained("minishlab/potion-base-8M")
  embeddings = model.encode(["doc one", "doc two", "query"])   # numpy 2-D array
  ```
- **Offline behavior**: downloads the static model from HF Hub on first use, then offline. Honors `HF_HUB_OFFLINE=1`.
- **Latency ballpark**: **fastest by far** — static embeddings are ~50-500x faster than transformer inference because there's no model forward pass, just token lookups + mean pooling. Essentially instant for a few-dozen-doc corpus. Cold start (import) is also near-instant (no torch/onnx import).
- **Trade-off**: static embeddings are a notch below MiniLM/BGE-small on retrieval quality benchmarks, but for *coarse semantic recall over a few dozen short memory notes* (where the current baseline is literal lexical overlap) this is more than enough and is a large quality jump over today's lexical scorer.

---

## Comparison summary

| | fastembed 0.8.0 | sentence-transformers 5.5.1 | model2vec 0.8.2 |
|---|---|---|---|
| Backend | ONNX Runtime (CPU) | PyTorch | static lookup (numpy only) |
| Pulls torch? | **No** | **Yes (123 MB win / 532 MB linux)** | **No** (inference path) |
| Install footprint | ~35-45 MB | **~200 MB win / 700 MB+ linux** | **~15-20 MB** |
| Brings numpy? | Yes | Yes | Yes |
| Default model | BAAI/bge-small-en-v1.5 | all-MiniLM-L6-v2 | potion-base-* (explicit) |
| Default dim | 384 | 384 | 256/512 (model-dependent) |
| Model download (1st use) | ~80-130 MB | ~90 MB | single-digit to ~30 MB |
| embed API | `list(TextEmbedding().embed(strs))` | `SentenceTransformer(m).encode(strs)` | `StaticModel.from_pretrained(m).encode(strs)` |
| Returns | generator of numpy arrays | numpy 2-D array | numpy 2-D array |
| Cold-start (import) | ~1-2 s ONNX init | **slow (2-5 s torch import)** | near-instant |
| Embed latency (short str) | low-single-digit ms | few ms | sub-ms (static) |
| Retrieval quality | high | high | good (below transformers) |
| Windows wheels | native cp312 | native cp312 | pure-py + tokenizers wheel |
| License | Apache-2.0 | Apache-2.0 | MIT |

---

## Recommendation for THIS use case

**Pick `fastembed`.** It is the best fit for a personal CLI optional extra:

- **No torch** — avoids the single biggest footprint problem (sentence-transformers drags in 123 MB on Windows / 700 MB+ on Linux, plus a slow 2-5 s import, for the *exact same 384-dim MiniLM-class output*).
- **Modest, predictable footprint** (~35-45 MB, dominated by onnxruntime + numpy) with native Windows cp312 wheels — no compiler, no CUDA.
- **Brings numpy transitively**, so cosine similarity is free; we do NOT need to add numpy separately to the extra.
- **High retrieval quality** (BGE-small-en-v1.5, 384-dim) — a real semantic upgrade over the current lexical scorer.
- **Clean offline story** (HF cache, `HF_HUB_OFFLINE=1`) and a simple `embed(list[str])` API.

**model2vec is a strong runner-up** if download size / cold-start latency matters more than top-tier retrieval quality (e.g. you want the extra to stay under ~20 MB and embeddings to be effectively instant). It is genuinely lighter and viable, and is a clean fallback. **sentence-transformers is not recommended** for this use case — same output dimension/quality tier as fastembed but multiples of the footprint and a heavy cold start, with no benefit for a tiny corpus.

Because numpy is **not** in the project today and the feature must **degrade gracefully**, the implementation should keep all embedding imports behind a lazy import inside `recall()` (mirroring the `pypdf`/`multilspy` lazy-import pattern used by the `pdf`/`lsp` extras) and fall back to the existing lexical `recall()` when fastembed/numpy is not installed.

### Exact `[embeddings]` extra to add to `pyproject.toml`

Add under `[project.optional-dependencies]` (alongside the existing `lsp` / `pdf` extras at lines 30-31):

```toml
embeddings = ["fastembed>=0.8"]
```

`fastembed` transitively pulls `onnxruntime`, `numpy`, `tokenizers`, and `huggingface-hub`, so **no separate numpy entry is required**. (If you want to pin numpy explicitly for cosine clarity you *may* add `"numpy>=1.26"`, but it is redundant given fastembed's own `numpy>=1.26` constraint on py3.12.)

If choosing model2vec instead, the line would be `embeddings = ["model2vec>=0.8"]` (numpy also comes transitively).

### Minimal embed-call snippet (fastembed)

```python
import numpy as np
from fastembed import TextEmbedding

_model = TextEmbedding()  # default BAAI/bge-small-en-v1.5, 384-dim; lazy-init once

def embed(texts: list[str]) -> list[list[float]]:
    """Embed a list of strings into 384-dim vectors (offline after first model fetch)."""
    return [vec.tolist() for vec in _model.embed(texts)]

def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12))
```

For recall: embed each memory's `name + description` (cache the doc vectors), embed the query, rank by cosine — a direct replacement for the `_relevance(...)` lexical score inside `recall()` with the surrounding scan/sort/`recall_section` rendering unchanged.

---

## Caveats / Not Found

- **Wheel sizes are *download* sizes**, not on-disk installed sizes; installed footprint is somewhat larger (unpacked). The *relative* ordering (model2vec << fastembed << sentence-transformers) holds either way.
- **Model download sizes (80-130 MB for BGE-small, ~90 MB for MiniLM)** are approximate from general knowledge of these models — the precise quantized ONNX size depends on fastembed's chosen quantization. Could not fetch exact HF model file sizes in this environment; verify on first install if a hard cap matters.
- **Latency figures are ballpark estimates** based on known characteristics of ONNX-CPU MiniLM/BGE-small vs torch-CPU vs static embeddings; not measured on the target laptop. The qualitative ordering (static fastest, torch slowest cold-start) is robust.
- **Web search MCP (exa) was unavailable** in this session; findings rely on the PyPI JSON API (authoritative for versions/deps/wheel sizes) plus PyPI long-description excerpts (authoritative for default model names / API shape). All version numbers, dependency declarations, and wheel sizes are live as of 2026-06-08.
- `transformers` latest is `5.10.2` (sentence-transformers pins `<6.0,>=4.41`, so it would resolve to a 5.x); not load-bearing for the recommendation.
