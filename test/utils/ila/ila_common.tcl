proc dcim_progress {message} {
    puts "DCIM_CAPTURE $message"
    flush stdout
}

# Native ILA waveform time is one fs per sample index.
proc mark_tops_output_window {start end} {
    set config [current_wave_config]
    add_wave_marker -into $config -name OUT16_START $start fs
    add_wave_marker -into $config -name OUT16_END $end fs
}

# One INT8 job is written every two samples. The ninth write closes the
# half-open window containing eight jobs. Check all three stages on the trace.
proc tops_eight_job_window {csvText} {
    package require csv
    set lines [split [string trim $csvText] "\n"]
    set names {}
    foreach name [csv::split [lindex $lines 0]] {
        set name [lindex [split [file tail [string trim $name]] "\["] 0]
        lappend names [regsub {^dcim_array_0_} $name {}]
    }
    set radices [csv::split [lindex $lines 1]]
    set columns {}
    set fullValues {}
    foreach aliases {{peak_compute_mask issue_mask} {peak_ma_fire_mask ma_accept_mask} {peak_result_valid_mask write_done_mask}} {
        set idx -1
        foreach alias $aliases {
            set idx [lsearch -exact $names $alias]
            if {$idx >= 0} break
        }
        if {$idx < 0} { return {} }
        lappend columns $idx
        switch -nocase -- [string trim [lindex $radices $idx]] {
            hex {lappend fullValues ff}
            bin - binary {lappend fullValues 11111111}
            oct {lappend fullValues 377}
            signed {lappend fullValues -1}
            unsigned - dec {lappend fullValues 255}
            default {return {}}
        }
    }
    set sampleCol [lsearch -nocase -exact $names "Sample in Buffer"]
    if {$sampleCol < 0} { return {} }
    set active {}
    set writes {}
    foreach line [lrange $lines 2 end] {
        set row [csv::split [string trim $line]]
        set sample [lindex $row $sampleCol]
        if {![string is integer -strict $sample] || $sample != [llength $active]} { return {} }
        set masks {}
        foreach i $columns full $fullValues {
            set v [string tolower [string trim [lindex $row $i]]]
            lappend masks [expr {$v eq $full ? 1 : [regexp {^0+$} $v] ? 0 : -1}]
        }
        lassign $masks issue ma write
        lappend active [expr {$issue == 1 && $ma == 1}]
        lappend writes $write
    }
    for {set start 0} {$start + 16 < [llength $writes]} {incr start} {
        if {[lindex $writes $start] != 1} continue
        for {set offset 0} {$offset <= 16} {incr offset} {
            set sample [expr {$start + $offset}]
            if {[lindex $writes $sample] != (1 - $offset % 2)
                || ($offset < 16 && ![lindex $active $sample])} break
        }
        if {$offset == 17} { return [list $start [expr {$start + 16}]] }
    }
    return {}
}

# Work on the viewer's WCFG copy. Preserve unrelated markers and probes.
proc prepare_tops_view {archive viewConfig destination} {
    package require zipfile::decode
    package require tdom
    zipfile::decode::open $archive
    try {
        set zip [zipfile::decode::archive]
        set native [lindex [lsearch -all -inline -glob [zipfile::decode::files $zip] *.wcfg] 0]
        set original [dom parse [zipfile::decode::getfile $zip $native]]
        set bounds {}
        if {[zipfile::decode::hasfile $zip waveform.csv]} {
            set bounds [tops_eight_job_window [zipfile::decode::getfile $zip waveform.csv]]
        }
    } finally { zipfile::decode::close }

    set input [open $viewConfig r]
    try {set view [dom parse -channel $input]} finally {close $input}
    try {
        set root [$view documentElement]
        foreach old [$root selectNodes {wave_markers/marker[@label="OUT16_START" or @label="OUT16_END" or @label="OUT8_START" or @label="OUT8_END"]}] {
            $old delete
        }
        set clkPath {wvobject[contains(@fp_name,"peak_clk_toggle")]}
        set clock [$root selectNodes $clkPath]
        if {![llength $clock]} {
            set originalClock [[$original documentElement] selectNodes $clkPath]
            if {[llength $originalClock]} {
                $root appendXML [[lindex $originalClock 0] asXML]
                set clock [$root selectNodes $clkPath]
            }
        }
        if {[llength $clock]} {
            set first [$root selectNodes {wvobject[1]}]
            if {$clock ne $first} { $root insertBefore [lindex $clock 0] $first }
            foreach size [$root selectNodes WVObjectSize] {
                $size setAttribute size [llength [$root selectNodes wvobject]]
            }
        }
        set out [open $destination w]
        try {puts $out [$view asXML]} finally {close $out}
    } finally { $view delete; $original delete }
    return $bounds
}

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
    dcim_progress "Connecting to Vivado Hardware Manager and JTAG"
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
