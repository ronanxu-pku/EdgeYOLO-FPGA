# Vivado sets its own Python environment. XDMA uses the host interpreter.
proc run_dcim_host_python {python args} {
    set original [dict create]
    set names {PATH PYTHONHOME PYTHONPATH}
    foreach name $names {
        if {![catch {set ::env($name)} current]} { dict set original $name $current }
        if {[catch {set ::env(DCIM_ILA_HOST_$name)} value] || $value eq ""} {
            unset -nocomplain ::env($name)
        } else {
            set ::env($name) $value
        }
    }
    try {
        exec $python -X faulthandler {*}$args 2>@1
    } finally {
        foreach name $names { unset -nocomplain ::env($name) }
        dict for {name value} $original { set ::env($name) $value }
    }
}

proc with_dcim_hw_device {body} {
    open_hw_manager
    try {
        connect_hw_server -url $::env(DCIM_ILA_HW_SERVER)
        set targets [get_hw_targets -quiet $::env(DCIM_ILA_HW_TARGET)]
        if {[llength $targets] != 1} {
            error "Expected exactly one JTAG target. Found: $targets. Check the cable or set --hw-target."
        }
        open_hw_target [lindex $targets 0]
        set devices [get_hw_devices -quiet xcvu37p*]
        if {[llength $devices] != 1} {
            error "Expected one VCU128 (xcvu37p) on the target. Found: [get_hw_devices -quiet]."
        }
        set device [lindex $devices 0]
        current_hw_device $device
        set_property PROBES.FILE $::env(DCIM_ILA_LTX) $device
        set_property FULL_PROBES.FILE $::env(DCIM_ILA_LTX) $device
        refresh_hw_device -update_hw_probes true $device
        uplevel 1 $body
    } finally {
        catch {close_hw_target}
        catch {disconnect_hw_server}
        close_hw_manager
    }
}
