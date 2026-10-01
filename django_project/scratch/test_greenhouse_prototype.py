import requests, html
from concurrent.futures import ThreadPoolExecutor

boards = ['stripe', 'figma', 'anthropic', 'datadog', 'cloudflare', 'coinbase', 'brex', 'reddit']

def fetch_board(b):
    try:
        r = requests.get(f'https://boards-api.greenhouse.io/v1/boards/{b}/jobs', timeout=4)
        if r.status_code == 200:
            return b, r.json().get('jobs', [])
    except Exception as e:
        pass
    return b, []

with ThreadPoolExecutor(max_workers=8) as ex:
    board_jobs = list(ex.map(fetch_board, boards))

all_jobs = []
for b, jobs in board_jobs:
    for j in jobs:
        j['board'] = b
        all_jobs.append(j)

print(f'Total fetched: {len(all_jobs)} jobs across {len(boards)} boards')
matches = [j for j in all_jobs if 'engineering manager' in j.get('title', '').lower()]
print(f'Matches for engineering manager: {len(matches)}')
for m in matches[:5]:
    board = m['board']
    title = m.get('title')
    loc = m.get('location', {}).get('name')
    url = m.get('absolute_url')
    print(f' - [{board}] {title} | {loc} | {url}')
