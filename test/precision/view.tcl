# Open the original samples using the Vivado logic analyzer and its native wave config.
open_hw_manager
set root $::env(DCIM_PRECISION_VIEW_ROOT)
foreach mode $::env(DCIM_PRECISION_VIEW_CASES) {
    set data [read_hw_ila_data [file join $root "$mode.ila"]]
    display_hw_ila_data -wcfg [file join $root "$mode.wcfg"] $data
}
