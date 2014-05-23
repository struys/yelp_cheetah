
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



wrapper=gdb,-x,/usr/lib/debug/usr/bin/python2.7-dbg-gdb.py,-ex,r,--args
export CC := /home/buck/trees/theirs/gcc-python-plugin/gcc-with-cpychecker --maxtrans=1000000 --dump-json -wrapper $(wrapper)

.PHONY: refcount
refcount:
	rm -rf build/lib*
	rm -f build/temp.linux-x86_64-2.7/*.{o,html,json}
	python setup.py build
	find build -regex '.*\.\(html\|json\)' | xargs git add -f
