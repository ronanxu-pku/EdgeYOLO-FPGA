# Open the original samples using the Vivado logic analyzer and its native wave config.
set root $::env(DCIM_PRECISION_VIEW_ROOT)
if {[catch {
    open_hw_manager
    foreach mode $::env(DCIM_PRECISION_VIEW_CASES) {
        set archive [file join $root "$mode.ila"]
        set config [file join $root "$mode.wcfg"]
        set data [read_hw_ila_data $archive]
        if {$mode ne "peak_int8"} {
            display_hw_ila_data -wcfg $config $data
            continue
        }
        source [file join [file dirname [info script]] ila_common.tcl]
        set displayConfig [file join $root peak_int8_display.wcfg]
        set bounds [prepare_tops_view $archive $config $displayConfig]
        display_hw_ila_data -wcfg $displayConfig $data
        set exporting [expr {[info exists ::env(DCIM_TOPS_SAVE_MARKERS)] && [info exists ::env(DCIM_TOPS_VIEW_WINDOW)]}]
        if {[llength $bounds]} {
            lassign $bounds start end
            if {!$exporting} {
                set wcfg [current_wave_config]
                add_wave_marker -into $wcfg -name OUT8_START $start fs
                add_wave_marker -into $wcfg -name OUT8_END $end fs
            }
            set cycles [expr {$end - $start}]
            set ops [expr {8 * 128 * 64 * 2}]
            dcim_progress [format {Tcl OUT8: samples [%d, %d), 8 jobs, %d cycles, %d OP; at 250 MHz: %g ns, %.3f TOPS} \
                               $start $end $cycles $ops [expr {$cycles * 4.0}] [expr {$ops * 250.0 / ($cycles * 1e6)}]]
        } else {
            dcim_progress "No verified 8-job window in this capture"
        }
        if {$exporting} {
            # The existing Python exporter consumes its original marker pair.
            # Add that pair only in the separate batch export session.
            mark_tops_output_window {*}$::env(DCIM_TOPS_VIEW_WINDOW)
            save_wave_config [file join $root marked.wcfg]
        }
    }
} message]} {
    set log [open [file join $root view_error.txt] w]
    puts $log $message
    close $log
    exit 1
}
