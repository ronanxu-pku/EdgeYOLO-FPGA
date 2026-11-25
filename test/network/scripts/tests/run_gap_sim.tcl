# vivado -mode batch -source test/network/scripts/tests/run_gap_sim.tcl
set repo [file normalize [file join [file dirname [info script]] ../../../..]]
set build [file join $repo test/network/cached/gap_rtl]
create_project -force gap_rtl $build -part xcvu37p-fsvh2892-2L-e
set_property target_language Verilog [current_project]
set_property source_mgmt_mode None [current_project]
create_ip -name floating_point -vendor xilinx.com -library ip -module_name fp32_add
set_property -dict [list CONFIG.Operation_Type {Add_Subtract} CONFIG.Add_Sub_Value {Add} \
    CONFIG.A_Precision_Type {Single} CONFIG.Result_Precision_Type {Single} \
    CONFIG.Flow_Control {NonBlocking} CONFIG.Has_ARESETn {false}] [get_ips fp32_add]
generate_target simulation [get_ips fp32_add]
add_files [file join $repo project/rtl/vpu/mp_unit_fixed.sv]
set_property include_dirs [list [file join $repo project/rtl/chip]] [get_filesets sources_1]
set_property top mp_unit_fixed [get_filesets sources_1]
add_files -fileset sim_1 [file join $repo test/network/scripts/tests/tb_gap.sv]
set_property source_set sources_1 [get_filesets sim_1]
set_property top tb_gap [get_filesets sim_1]
set_property xsim.simulate.runtime 0ns [get_filesets sim_1]
launch_simulation
run all
if {[get_value /tb_gap/all_pass] != 1} { error "GAP RTL validation failed" }
close_sim
close_project
