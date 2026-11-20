# Invoked by run.py in a batch Vivado session on the XDMA host.
set ::precisionTestDir [file dirname [file normalize [info script]]]

source [file join $::precisionTestDir .. ila_common.tcl]

proc capture_dcim_precision {python mode runDir workerArgs} {
    set runDir [file normalize $runDir]
    if {$mode ni {int8 int16 int16-high}} {
        error "Mode must be int8, int16, or int16-high."
    }
    if {[file exists $runDir]} {
        error "Use a new capture directory: $runDir"
    }
    set ila [get_hw_ilas -of_objects [current_hw_device] -filter {CELL_NAME =~ "*precision_ila*"}]
    if {[llength $ila] != 1} {
        error "Expected precision_ila on the FPGA. Check the programmed bitstream and its matching .ltx."
    }
    set stage [get_hw_probes -of_objects $ila -filter {NAME =~ "*prec_stage_valid*"}]
    if {[llength $stage] != 1} {
        error "The precision stage-valid probe was not found in the loaded .ltx."
    }

    # The host issues one job after arming. The first high sample is phase 0.
    reset_hw_ila $ila
    set_property CONTROL.DATA_DEPTH 1024 $ila
    set_property CONTROL.TRIGGER_POSITION 32 $ila
    set_property TRIGGER_COMPARE_VALUE {eq5'bXXXX1} $stage
    run_hw_ila $ila
    set hostFailed [catch {
        run_dcim_host_python $python [file join $::precisionTestDir run.py] --case $mode --run-dir $runDir --xdma-only {*}$workerArgs
    } hostLog]
    file mkdir $runDir
    set logFile [open [file join $runDir host.log] w]
    puts $logFile $hostLog
    close $logFile
    puts $hostLog
    set captureFailed [catch {
        wait_on_hw_ila -timeout 1 $ila
        set data [upload_hw_ila_data $ila]
        write_hw_ila_data -csv_file [file join $runDir precision.csv] $data
        # Keep the native archive separate from the temporary CSV export.
        set archive [file join $runDir capture.ila]
        write_hw_ila_data $archive $data
        if {![file isfile $archive] || [file size $archive] == 0} {
            error "Vivado did not save the native ILA archive: $archive"
        }
    } captureLog]
    if {$captureFailed} { puts stderr "ILA capture failed: $captureLog" }
    if {$hostFailed} { error "XDMA check failed. See $runDir/host.log." }
    if {$captureFailed} { error $captureLog }
    puts [run_dcim_host_python $python [file join $::precisionTestDir check_capture.py] $runDir]
}

if {[info exists ::env(DCIM_PRECISION_CASES)]} {
    with_dcim_hw_device {
        set workerArgs [list --timeout-s $::env(DCIM_PRECISION_TIMEOUT)]
        if {$::env(DCIM_PRECISION_QUIET)} { lappend workerArgs --quiet-xdma }
        foreach mode $::env(DCIM_PRECISION_CASES) {
            capture_dcim_precision $::env(DCIM_ILA_PYTHON) $mode [file join $::env(DCIM_PRECISION_CAPTURE_ROOT) $mode] $workerArgs
        }
    }
}
