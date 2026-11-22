#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
trap 'rc=$?; printf "%s\n" "$rc" > exit_status' EXIT
unset EDGEYOLO_NOTIFY_EMAIL EDGEYOLO_NOTIFY_FROM EDGEYOLO_NOTIFY_AUTH
unset FORCE_BITSTREAM DCIM_BD_VALIDATE_ONLY BD_VALIDATE_ONLY STOP_AFTER RESUME_FROM SOURCE_TAG
export GIT_CEILING_DIRECTORIES="$(dirname "$PWD")"
export BUILD_TAG=@PROFILE@_ila_@COMMIT@
export FLOW_MODE=project IMPL_FLOW=two_stage
export VIVADO="${VIVADO:-/home/EDAtools/Xilinx/Vivado/2024.2/bin/vivado}"
export SYNTH_JOBS="$(nproc)" VIVADO_THREADS=32 PLACE_THREADS=32 ROUTE_THREADS=32
export IMPL_ROUTE_TOP_K=4
export DCIM_PRECISION_ILA=0
if [[ "@PROFILE@" == precision ]]; then export DCIM_PRECISION_ILA=1; fi
"$VIVADO" -mode batch -source scripts/chip-lite/run.tcl -log vivado.log -journal vivado.jou
