#!/bin/bash
S=${S:-/tmp/dispatch-more-gate}; mkdir -p "$S"
R="$(cd "$(dirname "$0")/../.." && pwd)"; E=$HOME/.cache/dispatch-more-test; N=${1:-gate34}; REL=${2:-v282}
cd "$R"; G=$S/$N.log; : > $G
echo "commit $(git rev-parse --short HEAD) on $(git branch --show-current)" >> $G
POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=55432 POSTGRES_USER=postgres POSTGRES_PASSWORD=postgres POSTGRES_DB=dispatcharr_claude DJANGO_SECRET_KEY=x DISPATCHARR_ENV=aio DISPATCHARR_LOG_LEVEL=WARNING timeout 3000 $E/venv/bin/python manage.py test --noinput > $S/$N-backend.log 2>&1
echo "backend: $(grep -a -E '^Ran ' $S/$N-backend.log) $(grep -a -E '^FAILED|^OK' $S/$N-backend.log)" >> $G
grep -a -E "^(ERROR|FAIL): " $S/$N-backend.log | sed -E 's/ \(.*//' | sort > $S/$N-errors.txt
if diff -q fork/patcher/baseline-034-errors.txt $S/$N-errors.txt >/dev/null; then echo "backend errors: the 151 of the 0.34 baseline, by name" >> $G; else echo "backend errors DIFFER from the baseline:" >> $G; diff fork/patcher/baseline-034-errors.txt $S/$N-errors.txt >> $G; fi
(cd frontend && timeout 2400 npx vitest run > $S/$N-frontend.log 2>&1)
echo "frontend: $(grep -aE 'Tests ' $S/$N-frontend.log | tail -1)" >> $G
grep -aE "FAIL " $S/$N-frontend.log | head -10 >> $G
(cd frontend && timeout 900 npm run build > $S/$N-build.log 2>&1) && echo "build: ok" >> $G || { echo "build: FAILED" >> $G; tail -15 $S/$N-build.log >> $G; }
git status --porcelain >> $G
timeout 2700 bash fork/patcher/release.sh $REL > $S/$N-release.log 2>&1 && echo "release.sh $REL (overlay + test-patcher): ok -- $(tail -1 $S/$N-release.log)" >> $G || { echo "release.sh $REL: FAILED -- $(tail -1 $S/$N-release.log)" >> $G; grep -a "FAIL" $S/$N-release.log | head -20 >> $G; }
echo done >> $G
