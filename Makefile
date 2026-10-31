.PHONY: build test contracts check
build:
	mkdir -p bin
	go build -trimpath -o bin/cgit-runtime ./cmd/cgit-runtime

test:
	go test -race -count=1 ./...

contracts: build
	python3 scripts/test.py --driver cgi

check:
	go vet ./...
	go mod verify
	shellcheck docker-entrypoint.sh docker-ssh-entrypoint.sh docker-maintenance-entrypoint.sh
	python3 scripts/check_contracts.py --stage go
	git diff --check
