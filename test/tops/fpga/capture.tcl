set testDir [file dirname [file normalize [info script]]]
source [file join $testDir .. .. ila_common.tcl]

with_dcim_hw_device {
    set ila [get_hw_ilas -of_objects [current_hw_device] -filter {CELL_NAME =~ "*/peak_tops_ila"}]
    if {[llength $ila] != 1} {
        error "Expected peak_tops_ila on the FPGA. Check the programmed combined bitstream and its matching top.ltx."
    }
    set mask [get_hw_probes -of_objects $ila -filter {NAME =~ "*issue_mask*"}]
    set index [get_hw_probes -of_objects $ila -filter {NAME =~ "*issue_phase_index*"}]
    if {[llength $mask] != 1 || [llength $index] != 1} {
        error "The TOPS issue probes are missing. Load the combined top.ltx."
    }
    reset_hw_ila $ila
    set_property CONTROL.DATA_DEPTH 1024 $ila
    set_property CONTROL.TRIGGER_POSITION 32 $ila
    set_property CONTROL.TRIGGER_CONDITION AND $ila
    set_property TRIGGER_COMPARE_VALUE {eq8'hFF} $mask
    set_property TRIGGER_COMPARE_VALUE {eq7'h00} $index
    run_hw_ila $ila

    set folder $::env(DCIM_TOPS_CAPTURE_ROOT)
    set workerArgs [list --run-dir $folder --xdma-only --repeat-count 1 \
        --staging $::env(DCIM_TOPS_STAGING) --timeout-s $::env(DCIM_TOPS_TIMEOUT)]
    if {$::env(DCIM_TOPS_QUIET)} { lappend workerArgs --quiet-xdma }
    set failed [catch {
        run_dcim_host_python $::env(DCIM_ILA_PYTHON) [file join $testDir run.py] {*}$workerArgs
    } hostLog]
    puts $hostLog
    if {$failed} { error "TOPS XDMA execution or host readback check failed" }
    wait_on_hw_ila -timeout 1 $ila
    set data [upload_hw_ila_data $ila]
    write_hw_ila_data -csv_file [file join $folder peak_int8.csv] $data
    write_hw_ila_data [file join $folder peak_int8.ila] $data
}
