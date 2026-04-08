check:
	prek run --all-files

run:
	uv run uvicorn tinynvr.app:app --host 127.0.0.1 --port 8554 --reload

update:
	prek autoupdate
	uv sync --upgrade
