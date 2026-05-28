+++

title = "REAL_EPIC"

date = 2026-05-13

description = ""

[taxonomies]

tags = ["SLAM","无人机"] 

+++

## 建图

下面操作二选一

### 采集数据并建图

运行fastlio之后
`rosbag record -a`

播放 bag 包同时 `roslaunch fast_lio mapping_mid360.launch`

播放完 `ctrl+c` 就保存了

### 直接建图

先检查打开建图没

```bash
rosed fast_lio mapping_mid360.launch
# mapping_mid360.launch
<rosparam command="load" file="$(find fast_lio)/config/mid360.yaml" />

# mid360.yaml
# 保证打开
pcd_save_en: true
```

会保存在

## 仿真

```bash
# 比如叫 com_wood
roslaunch epic_planner com_wood.launch
```

```bash
# epic/com_wood.launch

# 参数单独设一份同名的
 <arg name="config_file" default="$(arg map_name).yaml" />
# 模拟也设一份
<include file="$(find epic_planner)/launch/simulation_$(arg map_name).launch"/>
```

```bash
# simulation_com_wood.launch

# 这里会转到 MARSIM 的launch
<include file="$(find mars_drone_sim)/launch/com_wood.launch">
```

```bash
# /(dir mar_sim)/com_wood.launch

# 地图改一下
<arg name="map_name" value="$(find map_generator)/resource/com_wood.pcd" />
# 这里放着 仿真参数
<include file="$(find test_interface)/launch/single_drone_with_fuel.xml">

# 如果要检测碰撞，找到这个文件 打开碰撞检测
# 把 0 改成 1
<param name="collisioncheck_enable" value="1" />
```

## 实飞

实飞注意设置飞行限制

### PX4Ctrl

#### 遥控档位

```C++
# 5
mode = ((double)msg.channels[4] - 1000.0) / 1000.0;
# 6
gear = ((double)msg.channels[5] - 1000.0) / 1000.0;
# 7
reboot_cmd = ((double)msg.channels[7] - 1000.0) / 1000.0;
```

`gear > 0.75` 时 `is_command_mode = true`

```
# 遥控通道对应
1 水平
2 前后
3 油门
4 YAW

5 mode
6 command 
7 Emergency stop
```

肩键是向下归1000，正面摇杆是向上归1000

想验证就开 `px4ctrl` ，检查 `/mavros/rc/in ` 同时拨动摇杆

### 启动逻辑

`epic_planner fsm_utils.cpp FastExplorationFSM::triggerCallback` 

第一次上升沿起飞（起飞后到指定高度），第二次规划

所以如果没在第一次上升沿时时正常起飞，此时进入 *PLAN_TRAJ*，但是 px4ctrl 还没起飞解锁

> 可以改一下，向上拖动只是发起飞指令，起飞后，向上拖动只检查是不是成功飞到指定高度了，是的话就开始规划

**PX4Ctrl**

开机在*MANUAL_CTRL*

当收到来自 epic 的上升沿，

```c++
  case TAKE_OFF:
  {
    for(int i = 0; i < 20; i++)
    {
      quadrotor_msgs::TakeoffLand takeoff_msg;
      takeoff_msg.takeoff_land_cmd = takeoff_msg.TAKEOFF;
      land_pub_.publish(takeoff_msg);
    }
    ROS_WARN("TAKE_OFF!!");
    transitState(WAIT_TRIGGER, "FSM");
  }

// 发布者名
land_pub_ = nh.advertise<quadrotor_msgs::TakeoffLand>("/px4ctrl/takeoff_land", 10);

// 句柄
ros::init(argc, argv, "px4ctrl");
ros::NodeHandle nh("~");
/*私有空间*/
// 在px4ctrl 中的接收者
ros::Subscriber takeoff_land_sub = nh.subscribe<quadrotor_msgs::TakeoffLand>("takeoff_land",100,boost::bind(&Takeoff_Land_Data_t::feed, &fsm.takeoff_land_data, _1),ros::VoidConstPtr(),ros::TransportHints().tcpNoDelay());

// 处理方法
void Takeoff_Land_Data_t::feed(quadrotor_msgs::TakeoffLandConstPtr pMsg)
{
    msg = *pMsg;
    rcv_stamp = ros::Time::now();

    triggered = true;
    takeoff_land_cmd = pMsg->takeoff_land_cmd;
    take_off_hight = pMsg->takeoff_hight;
    home_position[0] = pMsg->home_position_msg_x;
    home_position[1] = pMsg->home_position_msg_y;
    home_position[2] = pMsg->takeoff_hight;
}
```

```c++
if (mode > API_MODE_THRESHOLD_VALUE)
is_hover_mode = true;
else
is_hover_mode = false;
// is_hover_mode 就要 mode > 0.75 置于最高位

// PX4CtrlFSM.cpp
// 检查切换到 TAKEOFF
if (!rc_data.is_hover_mode || !rc_data.is_command_mode || !rc_data.check_centered())
// rc_data.is_hover_mode
/*
AUTO_TAKEOFF 只能从 MANUAL_CTRL 转入，如果拨杆过去会到 HOVER 进不去，只能初始置高位让其以初始状态进入 高杆量TAKEOFF
*/
```


### 启动

1. mode 拨到最上， gear ( 正面最右侧波杆 ) 拨到最下
2. rviz 里向上拖，即可正常解锁

