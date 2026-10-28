"""Private CPU RuBERT encoder. No LLM generation or external inference API."""

import os
import threading
from contextlib import asynccontextmanager

import torch
from fastapi import FastAPI
from pydantic import BaseModel, Field
from transformers import AutoModel, AutoTokenizer

MODEL = "cointegrated/rubert-tiny2"
REVISION = "e8ed3b0c8bbf4fb6984c3de043bf7d2f4e5969ae"
WINDOW = 512
LOCK = threading.Lock()
torch.set_num_threads(int(os.getenv("EMBEDDING_THREADS", "1")))
torch.set_num_interop_threads(1)


@asynccontextmanager
async def lifespan(app):
    global tokenizer, model
    tokenizer = AutoTokenizer.from_pretrained(MODEL, revision=REVISION, trust_remote_code=False)
    model = AutoModel.from_pretrained(MODEL, revision=REVISION, trust_remote_code=False, use_safetensors=True).eval()
    yield


app = FastAPI(title="Local RuBERT embeddings", lifespan=lifespan)


class Batch(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=32)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "model": MODEL,
        "revision": REVISION,
        "dimensions": 312,
        "pooling": "normalized CLS; normalized mean of overlapping windows for long text",
        "window_tokens": WINDOW,
        "device": "cpu",
    }


@app.post("/embed")
def embed(batch: Batch):
    if any(len(t) > 20000 for t in batch.texts):
        from fastapi import HTTPException

        raise HTTPException(422, "Each text must be at most 20000 characters")
    with LOCK, torch.inference_mode():
        windows = []
        owners = []
        for owner, text in enumerate(batch.texts):
            ids = tokenizer.encode(text, add_special_tokens=False, truncation=False)
            # Overlapping token windows cover the entire input, without silent truncation.
            for start in range(0, max(1, len(ids)), WINDOW - 2 - 64):
                part = ids[start : start + WINDOW - 2]
                windows.append(tokenizer.prepare_for_model(part, add_special_tokens=True))
                owners.append(owner)
                if start + WINDOW - 2 >= len(ids):
                    break
        sums = torch.zeros((len(batch.texts), 312))
        for offset in range(0, len(windows), 16):
            encoded = tokenizer.pad(windows[offset : offset + 16], padding=True, return_tensors="pt")
            vectors = torch.nn.functional.normalize(model(**encoded).last_hidden_state[:, 0, :], dim=1)
            for i, vector in enumerate(vectors):
                sums[owners[offset + i]] += vector
        vectors = torch.nn.functional.normalize(sums, dim=1).tolist()
    return {**health(), "embeddings": vectors}
