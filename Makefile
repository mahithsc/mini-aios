.PHONY: test updater-test updater-build mac-updater-demo

test:
	PYTHONPATH=. uv run --python 3.12 --with pytest pytest -q

updater-test:
	cd updater && go test ./...

updater-build:
	mkdir -p dist
	cd updater && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o ../dist/mini-aios-updater_linux_amd64 ./cmd/mini-aios-updater
	cd updater && CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -o ../dist/mini-aios-updater_linux_arm64 ./cmd/mini-aios-updater
	cd updater && CGO_ENABLED=0 GOOS=darwin GOARCH=amd64 go build -o ../dist/mini-aios-updater_darwin_amd64 ./cmd/mini-aios-updater
	cd updater && CGO_ENABLED=0 GOOS=darwin GOARCH=arm64 go build -o ../dist/mini-aios-updater_darwin_arm64 ./cmd/mini-aios-updater

mac-updater-demo:
	./scripts/mac-updater-demo.sh
