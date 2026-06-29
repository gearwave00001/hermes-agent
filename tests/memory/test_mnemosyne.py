#!/usr/bin/env python3
import os
os.environ['MNEMOSYNE_EMBEDDING_API_URL'] = 'http://192.168.1.225:5679/v1'
os.environ['MNEMOSYNE_EMBEDDING_MODEL'] = 'Qwen3-Embedding-8B-FP8-DYNAMIC'
os.environ['MNEMOSYNE_EMBEDDING_API_KEY'] = ''
os.environ['MNEMOSYNE_EMBEDDING_DIM'] = '4096'

from mnemosyne import Mnemosyne
m = Mnemosyne()

# Test store
result = m.remember('Test GPU embedding endpoint connection', importance=0.9, source='test')
print('Stored:', result)

# Test recall
results = m.recall('GPU endpoint connection', top_k=3)
print(f'Found {len(results)} results')
for r in results:
    if isinstance(r, dict):
        print(f'  Score: {r.get("score", "N/A"):.3f} | {r.get("text", r.get("content", ""))[:80]}')
    else:
        print(f'  Score: {getattr(r, "score", "N/A"):.3f} | {getattr(r, "text", "")[:80]}')

# Test stats
print()
print('Stats:')
stats = m.stats()
print(f'  Total: {stats}')
