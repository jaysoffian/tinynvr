ALPINE_VERSION := 3.15.11
MP4BOX_VERSION := 0.5.3

# To bump a version, edit the *_VERSION above and `rm` the file.
static/alpine.min.js:
	curl -sfL https://cdn.jsdelivr.net/npm/alpinejs@$(ALPINE_VERSION)/dist/cdn.min.js -o $@

static/mp4box.all.min.js:
	curl -sfL https://cdn.jsdelivr.net/npm/mp4box@$(MP4BOX_VERSION)/dist/mp4box.all.min.js -o $@

.PHONY: build
build: static/alpine.min.js static/mp4box.all.min.js
	podman build --platform linux/amd64 \
		--build-arg GIT_COMMIT=$$(git rev-parse --short HEAD) \
		-t tinynvr:latest .

.PHONY: image
image: build
	podman save -o tinynvr.tar tinynvr:latest

config.yaml: config.yaml.example
	test -f config.yaml || cp config.yaml.example config.yaml

.PHONY: run
run: build config.yaml
	mkdir -p recordings
	podman run --rm -it \
		-p 8554:8554 \
		-v $(PWD)/config.yaml:/config/config.yaml \
		-v $(PWD)/recordings:/recordings \
		-v $(PWD)/tinynvr:/app/tinynvr \
		-v $(PWD)/static:/app/static \
		tinynvr:latest \
		uvicorn tinynvr.app:app --host 0.0.0.0 --port 8554 --reload

.PHONY: .venv/setup
setup: .venv/.setup
.venv/.setup:
	mise install
	mise x -- prek install
	mise x -- uv sync
	touch "$@"

.PHONY: check
check: setup
	mise x -- prek run --all-files

.PHONY: update
update:
	mise x -- prek autoupdate
	mise x -- uv sync --upgrade
