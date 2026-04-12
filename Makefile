ALPINE_VERSION := 3.15.11
MP4BOX_VERSION := 0.5.3

check:
	prek run --all-files

run: static/alpine.min.js static/mp4box.all.min.js
	uv run uvicorn tinynvr.app:app --host 127.0.0.1 --port 8554 --reload

image: static/alpine.min.js static/mp4box.all.min.js
	podman build --platform linux/amd64 \
		--build-arg GIT_COMMIT=$$(git rev-parse --short HEAD) \
		-t tinynvr:latest .
	podman save -o tinynvr.tar tinynvr:latest

# Vendored JS deps are gitignored and fetched on demand. `make run`
# and `make image` depend on them so files are pulled automatically
# on first use. To bump a version, edit the *_VERSION above and run
# the matching refresh target to force a refetch.
static/alpine.min.js:
	curl -sfL https://cdn.jsdelivr.net/npm/alpinejs@$(ALPINE_VERSION)/dist/cdn.min.js -o $@

static/mp4box.all.min.js:
	curl -sfL https://cdn.jsdelivr.net/npm/mp4box@$(MP4BOX_VERSION)/dist/mp4box.all.min.js -o $@

.PHONY: alpine-refresh mp4box-refresh
alpine-refresh:
	rm -f static/alpine.min.js
	$(MAKE) static/alpine.min.js

mp4box-refresh:
	rm -f static/mp4box.all.min.js
	$(MAKE) static/mp4box.all.min.js

update:
	prek autoupdate
	uv sync --upgrade
