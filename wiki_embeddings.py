"""LangChain Embeddings backed by our private CPU encoder, with disk caching."""
import hashlib,json,os,sqlite3
from pathlib import Path
import httpx,numpy as np
from langchain_core.embeddings import Embeddings
from wiki_store import ClosingConnection

class LocalRuBertEmbeddings(Embeddings):
 def __init__(self,url=None,cache_dir=None):
  self.url=(url or os.getenv('EMBEDDINGS_URL','http://127.0.0.1:8082')).rstrip('/')
  self.cache_dir=Path(cache_dir or os.getenv('CACHE_DIR','.cache'));self.cache_dir.mkdir(parents=True,exist_ok=True)
  with httpx.Client(trust_env=False,timeout=30) as client:
   r=client.get(self.url+'/health');r.raise_for_status();self.info=r.json()
  identity={k:self.info[k] for k in ('model','revision','dimensions','pooling','window_tokens')}
  self.fingerprint=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
  with self._cache() as con:con.execute('CREATE TABLE IF NOT EXISTS vectors(key TEXT PRIMARY KEY,vector BLOB)')
 def _cache(self):return sqlite3.connect(self.cache_dir/'embedding-cache.sqlite',timeout=120,factory=ClosingConnection)
 def embed_documents(self,texts):
  keys=[hashlib.sha256((self.fingerprint+'\0'+t).encode()).hexdigest() for t in texts];result=[None]*len(texts)
  with self._cache() as con:
   for i,key in enumerate(keys):
    row=con.execute('SELECT vector FROM vectors WHERE key=?',(key,)).fetchone()
    if row:result[i]=np.frombuffer(row[0],dtype='<f4').tolist()
  missing=[i for i,v in enumerate(result) if v is None]
  with httpx.Client(trust_env=False,timeout=180) as client:
   for start in range(0,len(missing),16):
    batch=missing[start:start+16]
    r=client.post(self.url+'/embed',json={'texts':[texts[i] for i in batch]});r.raise_for_status();data=r.json()
    if any(data.get(k)!=self.info[k] for k in ('model','revision','dimensions','pooling','window_tokens')):raise ValueError('Embedding model identity changed; rebuild index')
    vectors=data['embeddings']
    if len(vectors)!=len(batch):raise ValueError('Wrong embedding batch size')
    with self._cache() as con:
     for i,vector in zip(batch,vectors):
      array=np.asarray(vector,dtype='<f4')
      if array.shape!=(self.info['dimensions'],) or not np.isfinite(array).all() or abs(np.linalg.norm(array)-1)>0.01:raise ValueError('Invalid or unnormalized embedding')
      result[i]=array.tolist();con.execute('INSERT OR REPLACE INTO vectors VALUES (?,?)',(keys[i],array.tobytes()))
  return result
 def embed_query(self,text):return self.embed_documents([text])[0]
