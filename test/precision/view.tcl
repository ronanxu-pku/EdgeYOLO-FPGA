# Open the original samples using the Vivado logic analyzer and its native wave config.
open_hw_manager
foreach mode $::env(DCIM_PRECISION_VIEW_CASES) {
    set data [read_hw_ila_data [file normalize "$mode.ila"]]
    display_hw_ila_data -wcfg [file normalize "$mode.wcfg"] $data
}
