
REBUILD_FLAG =

.PHONY: all
all: venv test

.PHONY: venv
venv: .venv.touch
	tox -e venv $(REBUILD_FLAG)

.PHONY: bench
bench:
	BENCH=true tox -e bench

.PHONY: tests test
tests: test
test: .venv.touch
	tox $(REBUILD_FLAG)


.venv.touch: setup.py requirements-dev.txt
	$(eval REBUILD_FLAG := --recreate)
	touch .venv.touch


.PHONY: clean
clean:
	find . -iname '*.pyc' -print0 | xargs -r0 rm -f
	rm -f Cheetah/_namemapper.so
	rm -rf .tox
	rm -rf ./venv-*
	rm -f .venv.touch