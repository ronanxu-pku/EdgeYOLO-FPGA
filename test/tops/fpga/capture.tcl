set testDir [file dirname [file normalize [info script]]]
source [file join $testDir .. .. ila_common.tcl]

with_dcim_hw_device {
    set ila [get_hw_ilas -of_objects [current_hw_device] -filter {CELL_NAME =~ "*/peak_tops_ila"}]
    if {[llength $ila] != 1} {
        error "Expected peak_tops_ila. Program top.bit and load top.ltx."
    }
    set mask [get_hw_probes -of_objects $ila -filter {NAME =~ "*issue_mask*"}]
    reset_hw_ila $ila
    set_property CONTROL.DATA_DEPTH 1024 $ila
    set_property CONTROL.TRIGGER_POSITION 32 $ila
    set_property CONTROL.TRIGGER_CONDITION AND $ila
    if {[llength $mask]} {
        set trigger {issue_mask eq8'hFF issue_phase_index eq7'h00}
    } else {
        set trigger {peak_compute_mask eq8'hFF peak_job eq6'h00 peak_phase eq2'h0}
    }
    foreach {signal compare} $trigger {
        set probe [get_hw_probes -of_objects $ila -filter [format {NAME =~ "*%s*"} $signal]]
        if {[llength $probe] != 1} { error "Expected one $signal probe. Check the programmed BIT/LTX pair." }
        set_property TRIGGER_COMPARE_VALUE $compare $probe
    }
    run_hw_ila $ila

    set folder $::env(DCIM_TOPS_CAPTURE_ROOT)
    set workerArgs [list --run-dir $folder --xdma-only --repeat-count 1 \
        --staging $::env(DCIM_TOPS_STAGING) --timeout-s $::env(DCIM_TOPS_TIMEOUT)]
    if {$::env(DCIM_TOPS_QUIET)} { lappend workerArgs --quiet-xdma }
    puts "DCIM_CAPTURE TOPS ILA armed, running XDMA"
    flush stdout
    set failed [catch {
        run_dcim_host_python $::env(DCIM_ILA_PYTHON) [file join $testDir .. run.py] {*}$workerArgs
    } hostLog]
    puts $hostLog
    if {$failed && ![file isfile [file join $folder fpga_peak_report.json]]} {
        error "TOPS XDMA execution or host readback check failed"
    }
    puts "DCIM_CAPTURE TOPS workload finished, reading the ILA capture"
    flush stdout
    wait_on_hw_ila -timeout 1 $ila
    set data [upload_hw_ila_data $ila]
    write_hw_ila_data -csv_file [file join $folder peak_int8.csv] $data
    write_hw_ila_data [file join $folder peak_int8.ila] $data
}
