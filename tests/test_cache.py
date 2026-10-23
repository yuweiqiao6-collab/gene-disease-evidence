import numpy as np
import pytest
import cache_manager
import mini_vector_store
from llm_cache import make_cache_key
from llm_client import LLMClient

CHUNK={'chunk_id':'c1','paper_id':'p1','context':'IL6 expression is high in arthritis.','title':'Test'}


def test_prompt_mode_and_source_metadata_are_in_cache_key():
    args=dict(model='test',base_url='https://invalid.test',gene='IL6',disease='arthritis',chunk=CHUNK)
    key=make_cache_key(**args)
    assert key!=make_cache_key(**args,use_cot=True)
    assert key!=make_cache_key(**{**args,'chunk':{**CHUNK,'title':'Updated title'}})


def test_client_does_not_reuse_other_prompt_modes(tmp_path,monkeypatch):
    import llm_client
    monkeypatch.setattr(llm_client,'get_llm_settings',lambda:dict(api_key='test',base_url='https://invalid.test',
        model='test',timeout=1,cache_enabled=True))
    client=LLMClient(cache_path=tmp_path/'cache.sqlite',allow_fallback=False)
    modes=[]
    monkeypatch.setattr(client,'_call_chat_completion',lambda g,d,c,use_cot=False: modes.append(use_cot) or [])
    client.extract('IL6','arthritis',CHUNK,use_cot=False)
    client.extract('IL6','arthritis',CHUNK,use_cot=True)
    client.extract('IL6','arthritis',CHUNK,use_cot=False)
    assert modes==[False,True]


def test_vector_content_model_revision_and_corruption_trigger_rebuild(tmp_path,monkeypatch):
    monkeypatch.setattr(cache_manager,'MINI_VECTOR_DIR',tmp_path/'vectors')
    monkeypatch.setattr(cache_manager,'PUBMED_CACHE_DIR',tmp_path/'pubmed')
    monkeypatch.setattr(cache_manager,'_CACHE_MANIFEST',tmp_path/'manifest.json')
    revision=['one']
    monkeypatch.setattr(cache_manager,'get_embedding_settings',lambda:dict(model_name='fake',model_revision=revision[0]))
    encoded=[]
    def embed(chunks,field):
        encoded.append(chunks)
        return np.array([[1.,0.] for c in chunks])
    monkeypatch.setattr(mini_vector_store,'embed_chunks',embed)
    mini_vector_store.get_or_build_mini_store('IL6','arthritis',[CHUNK])
    mini_vector_store.get_or_build_mini_store('IL6','arthritis',[CHUNK])
    assert len(encoded)==1
    changed={**CHUNK,'context':CHUNK['context']+' New text.'}
    mini_vector_store.get_or_build_mini_store('IL6','arthritis',[changed])
    assert len(encoded)==2
    revision[0]='two'
    mini_vector_store.get_or_build_mini_store('IL6','arthritis',[CHUNK])
    assert len(encoded)==3
    signature=cache_manager.mini_vector_signature([CHUNK])
    cache_manager.get_mini_vector_paths('IL6','arthritis',signature)[1].write_text('broken')
    mini_vector_store.get_or_build_mini_store('IL6','arthritis',[CHUNK])
    assert len(encoded)==3  # recover mini metadata from verified shared vectors
