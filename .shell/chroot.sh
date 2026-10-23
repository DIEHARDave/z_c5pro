#!/bin/sh
# (C) 2026 ghzserg https://github.com/ghzserg/zmod

source /usr/data/zmod/zmod/.shell/0.sh

unset LD_PRELOAD

if [ -f /ZMOD ]; then
    $@
else
    chroot ${MOD} $@
fi
