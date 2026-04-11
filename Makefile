check:
	prek run --all-files

run:
	uv run uvicorn tinynvr.app:app --host 127.0.0.1 --port 8554 --reload

image:
	podman build --platform linux/amd64 \
		--build-arg GIT_COMMIT=$$(git rev-parse --short HEAD) \
		-t tinynvr:latest .
	podman save -o tinynvr.tar tinynvr:latest

update:
	prek autoupdate
	uv sync --upgrade
