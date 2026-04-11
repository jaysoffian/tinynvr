ALPINE_VERSION := 3.15.11

check:
	prek run --all-files

run: static/alpine.min.js
	uv run uvicorn tinynvr.app:app --host 127.0.0.1 --port 8554 --reload

image: static/alpine.min.js
	podman build --platform linux/amd64 \
		--build-arg GIT_COMMIT=$$(git rev-parse --short HEAD) \
		-t tinynvr:latest .
	podman save -o tinynvr.tar tinynvr:latest

# Alpine.js is gitignored and fetched on demand. `make run` and `make
# image` depend on it so the file is pulled automatically on first
# use. To bump the version, edit ALPINE_VERSION above and run
# `make alpine-refresh` to force a refetch.
static/alpine.min.js:
	curl -sfL https://cdn.jsdelivr.net/npm/alpinejs@$(ALPINE_VERSION)/dist/cdn.min.js -o $@

.PHONY: alpine-refresh
alpine-refresh:
	rm -f static/alpine.min.js
	$(MAKE) static/alpine.min.js

update:
	prek autoupdate
	uv sync --upgrade
