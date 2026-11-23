# Convenience targets — everything also works with plain `docker compose`.
.PHONY: up down logs ps test scale health dashboard chaos burst clean demo-env

up:            ## build + start gateway, broker, redis and 2 workers
	docker compose up -d --build
down:
	docker compose down
clean:         ## stop and wipe broker/redis volumes
	docker compose down -v
logs:
	docker compose logs -f --tail=100 worker gateway
ps:
	docker compose ps
scale:         ## make scale N=4
	docker compose up -d --scale worker=$(or $(N),3)
health:
	@curl -s localhost:8000/healthz | python3 -m json.tool
test:          ## unit + Redis integration tests inside the compose network
	docker compose run --rm --no-deps -v "$(CURDIR)/backend:/app" -e REDIS_URL=redis://redis:6379/15 worker \
	  sh -c "pip install -q pytest pytest-asyncio >/dev/null 2>&1; python -m pytest -q"
chaos:         ## make chaos URL=https://youtu.be/...  (kills the worker holding the job mid-frame-extraction)
	python3 scripts/nt.py chaos "$(URL)"
burst:         ## make burst URLS="url1 url2 ..."
	python3 scripts/nt.py burst $(URLS)
verify:        ## make verify JOB=<job_id> NOTION_TOKEN=...
	docker compose exec -e NOTION_TOKEN=$(NOTION_TOKEN) worker python -m notetaker.tools.verify $(JOB)
