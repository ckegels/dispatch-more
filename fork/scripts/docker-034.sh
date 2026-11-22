#!/bin/bash
# The installer on real machines: v281 (0.33.0) -> Dispatcharr 0.34.1 -> v282, five ways.
S=${S:-/tmp/dispatch-more-gate}; mkdir -p "$S"
R="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$R"
OLD=fork/patcher/out/dispatch-more-v281-dispatcharr-0.33.0.tar.gz
NEW=fork/patcher/out/dispatch-more-v282-dispatcharr-0.34.1.tar.gz
G=$S/docker34.log; : > $G
run() { name=$1; shift; ( "$@" > $S/d34-$name.out 2>&1; echo "$name: exit $? -- $(grep -aE '[0-9]+ passed, [0-9]+ failed' $S/d34-$name.out | tail -1)" >> $G; grep -a "FAIL" $S/d34-$name.out | head -10 >> $G ) & }
run real env LOG=$S/d34-real-detail.log bash fork/patcher/test-real.sh $OLD $NEW
run docker env LOG=$S/d34-docker-detail.log bash fork/patcher/test-docker.sh $OLD $NEW
run community env LOG=$S/d34-comm-detail.log bash fork/patcher/test-community.sh $OLD $NEW
wait
run realup env UPGRADE=1 LOG=$S/d34-realup-detail.log bash fork/patcher/test-real.sh $OLD $NEW
run commup env UPGRADE=1 LOG=$S/d34-commup-detail.log bash fork/patcher/test-community.sh $OLD $NEW
wait
echo done >> $G
