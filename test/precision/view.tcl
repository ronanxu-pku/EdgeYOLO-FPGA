# Open the original samples using the Vivado logic analyzer and its native wave config.
set root $::env(DCIM_PRECISION_VIEW_ROOT)
if {[catch {
    open_hw_manager
    foreach mode $::env(DCIM_PRECISION_VIEW_CASES) {
        set data [read_hw_ila_data [file join $root "$mode.ila"]]
        display_hw_ila_data -wcfg [file join $root "$mode.wcfg"] $data
    }
} message]} {
    set log [open [file join $root view_error.txt] w]
    puts $log $message
    close $log
    exit 1
}
