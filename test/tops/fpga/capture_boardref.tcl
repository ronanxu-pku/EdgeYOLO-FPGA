# Use with peak_tops_ila in bitstream/top.bit and its matching bitstream/top.ltx.
proc dcim_boardref_core {} {
    set ila [get_hw_ilas -of_objects [current_hw_device] -filter {CELL_NAME =~ "*/peak_tops_ila"}]
    if {[llength $ila] != 1} {
        error "Expected one TOPS ILA. Select the VCU128 with its matching board-reference BIT and LTX."
    }
    return [lindex $ila 0]
}

proc dcim_boardref_arm {} {
    set ila [dcim_boardref_core]
    reset_hw_ila $ila
    set_property CONTROL.DATA_DEPTH 1024 $ila
    set_property CONTROL.TRIGGER_POSITION 128 $ila
    set_property CONTROL.TRIGGER_CONDITION AND $ila
    foreach {signal compare} {peak_compute_mask eq8'hFF peak_job eq6'h00 peak_phase eq2'h0} {
        set probes [get_hw_probes -of_objects $ila -filter [format {NAME =~ "*%s*"} $signal]]
        if {[llength $probes] != 1} {
            error "Expected one $signal probe. Load top.ltx."
        }
        set_property TRIGGER_COMPARE_VALUE $compare [lindex $probes 0]
    }
    run_hw_ila $ila
    puts "TOPS ILA armed. Start the one-round INT8 preload test now."
}

proc dcim_boardref_save {folder} {
    set folder [file normalize $folder]
    set archive [file join $folder peak_int8.ila]
    set csv [file join $folder peak_int8.csv]
    set ila [dcim_boardref_core]
    wait_on_hw_ila -timeout 1 $ila
    set data [upload_hw_ila_data $ila]
    file mkdir $folder
    foreach path [list $archive $csv] {
        if {[file isfile $path]} { file delete $path }
    }
    write_hw_ila_data $archive $data
    write_hw_ila_data -csv_file $csv $data
    display_hw_ila_data $data
    puts "Saved native ILA capture $archive"
}
