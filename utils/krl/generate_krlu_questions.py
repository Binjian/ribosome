#!/usr/bin/env python3
"""Generate the deterministic, self-contained KRL understanding question bank.

Only writes the source question inventory, never collected answers. Regenerating
an inventory already used for collection changes prompt hashes, so review the
diff before collecting against existing output.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "assets/KRLU/krlu_questions_1000.md"


@dataclass(frozen=True)
class Example:
    assumptions: str
    declarations: str
    body: str
    questions: str
    extra_src: str = ""
    data: str = ""


TOPICS = []


def topic(title):
    def register(function):
        TOPICS.append((title, function))
        return function
    return register


def clean(text):
    return dedent(text).strip()


def b(value):
    return "TRUE" if value else "FALSE"


@topic("算术表达式与求值顺序")
def arithmetic(v, module):
    return Example("只执行本次调用，所有数值无溢出。", "DECL INT A,B,C,R1,R2\nDECL REAL R3", f"""
        A={7+v}
        B={2+v%4}
        C={3+v%5}
        R1=A+B*C
        R2=(A+B)*C
        R3=(A+B)/2.0
    """, "列出 R1、R2、R3 的值和数据类型，说明括号及 REAL 常量如何影响求值；把 R3 的分母换成 4.0 时，哪些变量会改变？")


@topic("布尔组合与否定范围")
def logic(v, module):
    flags = [b(v & (1 << bit)) for bit in range(5)]
    return Example("这是内存布尔量，不读取外部 I/O。", "DECL BOOL A,B,C,D,E,R1,R2", f"""
        A={flags[0]}
        B={flags[1]}
        C={flags[2]}
        D={flags[3]}
        E={flags[4]}
        R1=(A AND NOT B) OR (C AND D) OR E
        R2=NOT (A AND (B OR C)) AND (D OR NOT E)
    """, "逐项解释 R1、R2 的求值；只翻转 B 而不重新赋值其他输入时，两项结果是否改变？指出 NOT 的作用范围。")


@topic("阈值分支与等号边界")
def thresholds(v, module):
    limit = 10+v
    return Example("MEASURE 为无单位的模拟测量值。", "DECL INT MEASURE,CLASS", f"""
        MEASURE={limit + (-1,0,1,6)[v%4]}
        CLASS=0
        IF MEASURE<{limit} THEN
          CLASS=1
        ELSE
          IF MEASURE<={limit+5} THEN
            CLASS=2
          ELSE
            CLASS=3
          ENDIF
        ENDIF
    """, f"说明实际执行的分支和 CLASS；分别以 MEASURE={limit-1}、{limit}、{limit+5}、{limit+6} 重新调用时，分类边界怎样变化？")


@topic("嵌套互锁与错误优先级")
def nested(v, module):
    return Example("READY、CLAMPED、PART_OK 仅为普通工艺状态，不能代表安全许可。", "DECL BOOL READY,CLAMPED,PART_OK,START_OK\nDECL INT FAULT", f"""
        READY={b(v&1)}
        CLAMPED={b(v&2)}
        PART_OK={b(v&4)}
        FAULT={20+v}
        START_OK=FALSE
        IF NOT READY THEN
          FAULT=1
        ELSE
          IF NOT CLAMPED THEN
            FAULT=2
          ELSE
            IF PART_OK THEN
              FAULT=0
              START_OK=TRUE
            ELSE
              FAULT=3
            ENDIF
          ENDIF
        ENDIF
    """, "追踪 FAULT 和 START_OK；若 READY、CLAMPED 同时为 FALSE，哪个错误优先，为什么不能仅看最后一个 IF 推断结果？")


@topic("SWITCH分派与默认分支")
def switch(v, module):
    return Example("JOB 是本次调用选择的工艺编号。", "DECL INT JOB,TARGET", f"""
        JOB={v%6}
        TARGET={100+v}
        SWITCH JOB
        CASE 1
          TARGET=10
        CASE 2,3
          TARGET=20
        CASE 4
          TARGET=40
        DEFAULT
          TARGET=-1
        ENDSWITCH
        TARGET=TARGET+{v+1}
    """, "给出执行的 CASE 或 DEFAULT 及 TARGET 最终值；解释 CASE 2,3 的含义、是否需要 break，以及 JOB 改成 5 时的路径。")


@topic("正向FOR与STEP")
def forward_for(v, module):
    return Example("仅分析循环体实际访问的 I；不要求循环结束后 I 的值。", "DECL INT I,TOTAL,COUNT", f"""
        TOTAL={v}
        COUNT=0
        FOR I={1+v%3} TO {9+v} STEP {1+v%3}
          TOTAL=TOTAL+I
          COUNT=COUNT+1
        ENDFOR
    """, "列出循环体使用的 I 序列、COUNT 和 TOTAL；若上界增加 1，是否必定多执行一次？用该程序的 STEP 解释。")


@topic("反向FOR与倒序访问")
def reverse_for(v, module):
    return Example("数组索引从 1 开始，A[1] 至 A[5] 均已初始化。", "DECL INT I,A[5],TRACE", f"""
        A[1]={v+1}
        A[2]={v+2}
        A[3]={v+3}
        A[4]={v+4}
        A[5]={v+5}
        TRACE=0
        FOR I=5 TO 1 STEP -{1+v%2}
          TRACE=TRACE*10+I
          A[I]=A[I]+10
        ENDFOR
    """, "写出访问索引、TRACE 以及 A 的最终内容；解释负 STEP 的停止条件，并比较改成 STEP -2 后是否访问全部元素。")


@topic("WHILE入口判断")
def while_loop(v, module):
    return Example("STEP_SIZE 恒正，循环无外部状态变化。", "DECL INT VALUE,STEP_SIZE,COUNT", f"""
        VALUE={v%5}
        STEP_SIZE={1+v%4}
        COUNT=0
        WHILE VALUE<{8+v}
          VALUE=VALUE+STEP_SIZE
          COUNT=COUNT+1
        ENDWHILE
    """, "给出每次入口检查和最终 VALUE、COUNT；若初值恰好等于上界，是否进入循环？为什么最终值可能超过上界？")


@topic("REPEAT末尾判断")
def repeat_loop(v, module):
    return Example("循环体没有等待或运动，按顺序执行。", "DECL INT VALUE,COUNT", f"""
        VALUE={v+1}
        COUNT=0
        REPEAT
          VALUE=VALUE+{1+v%3}
          COUNT=COUNT+1
        UNTIL VALUE>={v+1+v%5}
    """, "求 VALUE 和 COUNT；说明 UNTIL 的真假分别导致继续还是结束，并分析初值已满足 UNTIL 时为什么仍要执行循环体。")


@topic("LOOP与EXIT退出点")
def endless_exit(v, module):
    return Example("此 LOOP 依靠 EXIT 结束，不依赖操作员停止。", "DECL INT N,TOTAL", f"""
        N=0
        TOTAL={v}
        LOOP
          N=N+1
          IF N>{3+v%5} THEN
            EXIT
          ENDIF
          TOTAL=TOTAL+N
        ENDLOOP
        TOTAL=TOTAL+100
    """, "区分 N 递增次数和 TOTAL 累加次数，列出 EXIT 前后执行的语句；若把累加移到 IF 前，最终 TOTAL 会怎样改变？")


@topic("双重FOR与行列索引")
def nested_for(v, module):
    return Example("M 是 3×4 整数数组，所有元素均在循环中赋值。", "DECL INT R,C,M[3,4],TOTAL", f"""
        TOTAL=0
        FOR R=1 TO 3
          FOR C=1 TO 4
            M[R,C]={v+1}*R+C
            TOTAL=TOTAL+M[R,C]
          ENDFOR
        ENDFOR
    """, "写出 M 的三行及 TOTAL；解释 M[R,C] 的两个索引，若将两层循环交换但保留赋值表达式，矩阵与累计值是否改变？")


@topic("内层EXIT与外层继续")
def inner_exit(v, module):
    return Example("只分析 TOTAL 和 COUNT，不使用循环退出后的索引值。", "DECL INT R,C,TOTAL,COUNT", f"""
        TOTAL={v}
        COUNT=0
        FOR R=1 TO {2+v%3}
          FOR C=1 TO 5
            IF C>{1+v%4} THEN
              EXIT
            ENDIF
            TOTAL=TOTAL+10*R+C
            COUNT=COUNT+1
          ENDFOR
        ENDFOR
    """, "列出真正累加的 (R,C) 对及 COUNT、TOTAL；EXIT 离开哪一层循环，外层的下一次迭代是否继续？")


@topic("RETURN保护分支与调用者")
def early_return(v, module):
    helper = f"CHECK_{module[-4:]}"
    return Example("两个 DEF 在同一 SRC 中，TRACE 由本次主程序初始化。", "", f"""
        TRACE={v}
        {helper}({v%5-2})
        TRACE=TRACE+100
    """, "追踪 TRACE；解释辅助过程中的 RETURN 返回哪里、是否跳过主程序末尾的 +100，并比较参数为 1 时的执行路径。", extra_src=f"""
        DEF {helper}(VALUE:IN)
          DECL INT VALUE
          IF VALUE<=0 THEN
            TRACE=TRACE+1
            RETURN
          ENDIF
          TRACE=TRACE+10
        END
    """, data="DECL INT TRACE=0")


@topic("GOTO与统一清理段")
def jump(v, module):
    return Example("FAULT 是局部仿真状态；标号只在本模块内使用。", "DECL BOOL FAULT\nDECL INT TRACE", f"""
        FAULT={b(v%2)}
        TRACE={v}
        IF FAULT THEN
          GOTO CLEANUP
        ENDIF
        TRACE=TRACE+10
        CLEANUP:
        TRACE=TRACE+1
    """, "说明跳转时跳过哪些语句、未跳转时是否仍进入 CLEANUP，并给出两种 FAULT 状态下的 TRACE；这里的标号是否相当于子程序调用？")


@topic("一维数组求和与筛选")
def array_sum(v, module):
    values = [v-2, 3-v, v+4, -(v+1), v%4]
    return Example("A[1..5] 是五个离散测量样本，不访问 A[0]。", "DECL INT I,A[5],SUM_ALL,SUM_POS", "\n".join(f"A[{i}]={x}" for i,x in enumerate(values,1))+"\n"+clean("""
        SUM_ALL=0
        SUM_POS=0
        FOR I=1 TO 5
          SUM_ALL=SUM_ALL+A[I]
          IF A[I]>0 THEN
            SUM_POS=SUM_POS+A[I]
          ENDIF
        ENDFOR
    """), "求 SUM_ALL、SUM_POS，指出零和负数分别走什么路径；若把 >0 改成 >=0，结果和执行路径是否都发生变化？")


@topic("数组极值与首次命中")
def array_max(v, module):
    values = [v+2, v+7, v-1, v+7, v+3]
    return Example("故意让两个元素具有相同最大值。", "DECL INT I,A[5],MAX_VALUE,MAX_INDEX", "\n".join(f"A[{i}]={x}" for i,x in enumerate(values,1))+"\n"+clean("""
        MAX_VALUE=A[1]
        MAX_INDEX=1
        FOR I=2 TO 5
          IF A[I]>MAX_VALUE THEN
            MAX_VALUE=A[I]
            MAX_INDEX=I
          ENDIF
        ENDFOR
    """), "按迭代列出极值更新点，给出 MAX_VALUE、MAX_INDEX；把 > 改成 >= 后会保留哪次最大值命中，为什么？")


@topic("数组移位与覆盖顺序")
def shift(v, module):
    return Example("两个数组初始内容相同，随后分别按不同方向原地移位。", "DECL INT I,A[4],B[4]", "\n".join(f"A[{i}]={v+i}\nB[{i}]={v+i}" for i in range(1,5))+"\n"+clean(f"""
        FOR I=4 TO 2 STEP -1
          A[I]=A[I-1]
        ENDFOR
        FOR I=2 TO 4
          B[I]=B[I-1]
        ENDFOR
        A[1]={30+v}
        B[1]={30+v}
    """), "写出每一步覆盖及 A、B 的最终值；为什么相同的赋值形式会产生不同结果，哪一个方向保留了原来的三个历史样本？")


@topic("二维数组行汇总")
def matrix_sum(v, module):
    return Example("M 表示两工位三批次的计数，ROW_SUM 为每工位合计。", "DECL INT R,C,M[2,3],ROW_SUM[2],TOTAL", "\n".join(f"M[{r},{c}]={v+r*3-c}" for r in range(1,3) for c in range(1,4))+"\n"+clean("""
        TOTAL=0
        FOR R=1 TO 2
          ROW_SUM[R]=0
          FOR C=1 TO 3
            ROW_SUM[R]=ROW_SUM[R]+M[R,C]
          ENDFOR
          TOTAL=TOTAL+ROW_SUM[R]
        ENDFOR
    """), "求两个 ROW_SUM 和 TOTAL；ROW_SUM 的清零语句位于内层循环外有什么作用，若移到内层每次赋值前会留下什么含义的数值？")


@topic("BOOL数组与命中计数")
def bool_array(v, module):
    values=[b((v+1)&(1<<i)) for i in range(6)]
    return Example("六个布尔量是已采样的工件检测结果，不会异步改变。", "DECL INT I,GOOD_COUNT,FIRST_BAD\nDECL BOOL GOOD[6]", "\n".join(f"GOOD[{i}]={x}" for i,x in enumerate(values,1))+"\n"+clean("""
        GOOD_COUNT=0
        FIRST_BAD=0
        FOR I=1 TO 6
          IF GOOD[I] THEN
            GOOD_COUNT=GOOD_COUNT+1
          ELSE
            IF FIRST_BAD==0 THEN
              FIRST_BAD=I
            ENDIF
          ENDIF
        ENDFOR
    """), "给出 GOOD_COUNT、FIRST_BAD；解释 FIRST_BAD==0 的作用及全部 GOOD 为 TRUE 时的结果，并分析去掉内层 IF 的影响。")


@topic("REAL递推滤波")
def filter_real(v, module):
    alpha = (v%4+1)/5
    return Example("仅处理三个已知 REAL 样本，不推断传感器物理量纲。", "DECL INT I\nDECL REAL RAW[3],FILTERED,ALPHA", f"""
        RAW[1]={10+v}.0
        RAW[2]={20+v}.0
        RAW[3]={5+v}.0
        ALPHA={alpha:.1f}
        FILTERED=0.0
        FOR I=1 TO 3
          FILTERED=ALPHA*RAW[I]+(1.0-ALPHA)*FILTERED
        ENDFOR
    """, "按三次迭代列出 FILTERED，说明它与三个样本算术平均值的区别；把 ALPHA 设为 1.0 后哪些历史信息被保留？")


@topic("ENUM状态与单次状态转移")
def enum_state(v, module):
    return Example("配套 DAT 定义用户枚举；输入状态每次主程序开始时明确赋值。", "DECL BOOL READY,DONE", f"""
        STATE=#{('IDLE','RUNNING','FINISHED')[v%3]}
        READY={b(v%2)}
        DONE={b(v%4>1)}
        COUNT={v}
        SWITCH STATE
        CASE #IDLE
          IF READY THEN
            STATE=#RUNNING
          ENDIF
        CASE #RUNNING
          COUNT=COUNT+1
          IF DONE THEN
            STATE=#FINISHED
          ENDIF
        CASE #FINISHED
          COUNT=0
        ENDSWITCH
    """, "给出本次调用后的 STATE、COUNT；在 #IDLE 分支内把 STATE 改成 #RUNNING 后，会不会在同一个 SWITCH 继续执行另一 CASE？", data="ENUM CYCLE_STATE IDLE,RUNNING,FINISHED\nDECL CYCLE_STATE STATE=#IDLE\nDECL INT COUNT=0")


@topic("STRUC字段与值复制")
def struct_copy(v, module):
    return Example("DAT 定义用户结构 WORK_REC；P1、P2 的字段均在本次运行设置。", "", f"""
        P1.ID={100+v}
        P1.HEIGHT={20+v}.0
        P1.GOOD={b(v%2)}
        P2=P1
        P2.ID=P2.ID+1
        P2.HEIGHT=P2.HEIGHT+{1+v%5}.0
        P1.GOOD=NOT P1.GOOD
    """, "列出 P1、P2 每个字段的最终值；P2=P1 后修改 P2 是否联动修改 P1，哪些字段仍然相同？", data="STRUC WORK_REC INT ID,REAL HEIGHT,BOOL GOOD\nDECL WORK_REC P1={ID 0,HEIGHT 0.0,GOOD FALSE}\nDECL WORK_REC P2={ID 0,HEIGHT 0.0,GOOD FALSE}")


@topic("DAT持久变量与局部变量")
def dat_lifetime(v, module):
    return Example(f"只在首次加载 DAT 时 COUNT={v}。随后在同一已加载模块中连续完整调用主程序两次；两次之间不重新加载、修改或初始化 DAT，也无外部写入。", "DECL INT LOCAL_COUNT", f"""
        LOCAL_COUNT={v+1}
        COUNT=COUNT+1
        LOCAL_COUNT=LOCAL_COUNT+COUNT
        LAST_RESULT=LOCAL_COUNT
    """, "分别给出第一次、第二次调用后的 COUNT 和 LAST_RESULT；解释局部变量每次显式赋值与 DAT 变量在两次调用间保留值的区别。", data=f"DECL INT COUNT={v}\nDECL INT LAST_RESULT=0")


@topic("子程序IN参数与调用顺序")
def subprogram_in(v, module):
    helper=f"ADD_{module[-4:]}"
    return Example("配套 DAT 的 TOTAL 被主程序清零；同一 SRC 内辅助过程可访问该模块数据。", "DECL INT VALUE", f"""
        VALUE={3+v}
        TOTAL=0
        {helper}(VALUE)
        VALUE=VALUE+1
        {helper}(VALUE)
    """, "按两次调用追踪形参与 TOTAL；过程内对 VALUE 的同名形参赋值会不会写回调用者的 VALUE？解释 :IN 的意义。", extra_src=f"""
        DEF {helper}(VALUE:IN)
          DECL INT VALUE
          VALUE=VALUE*2
          TOTAL=TOTAL+VALUE
        END
    """, data="DECL INT TOTAL=0")


@topic("DEFFCT返回值与分支")
def function_return(v, module):
    helper=f"LIMIT_{module[-4:]}"
    return Example("函数在同一 SRC 定义，所有实参为整数。", "DECL INT A,B,C", f"""
        A={helper}({v-5})
        B={helper}({v+3})
        C=A+B
    """, "给出 A、B、C；解释函数中的两个 RETURN 何时执行，返回值如何进入赋值表达式，以及过程 DEF 与 DEFFCT INT 的用途差别。", extra_src=f"""
        DEFFCT INT {helper}(VALUE:IN)
          DECL INT VALUE
          IF VALUE<0 THEN
            RETURN 0
          ENDIF
          RETURN VALUE*{2+v%3}
        ENDFCT
    """)


@topic("OUT参数与写回影响")
def output_parameter(v, module):
    helper=f"BUMP_{module[-4:]}"
    return Example("VALUE 实参是已初始化的 INT 变量，未给同一调用传递别名参数。", "DECL INT VALUE,BEFORE", f"""
        VALUE={10+v}
        BEFORE=VALUE
        {helper}(VALUE)
        {helper}(VALUE)
    """, "逐次给出调用后 VALUE、BEFORE；说明 :OUT 形参和主程序变量的关联，为什么 BEFORE 不随 VALUE 自动变化？", extra_src=f"""
        DEF {helper}(VALUE:OUT)
          DECL INT VALUE
          VALUE=VALUE+{1+v%5}
        END
    """)


@topic("CHAR数组与字符位置")
def character_array(v, module):
    label=f"CELL{v:02d}"
    return Example("TEXT 为足够长的 CHAR 数组，字符从索引 1 开始；只分析已赋值字符，不依赖未用空间的内容。", "DECL CHAR TEXT[20],FIRST,LAST", f"""
        TEXT[]="{label}"
        FIRST=TEXT[1]
        LAST=TEXT[6]
        TEXT[1]="Z"
        TEXT[5]="9"
    """, "给出 FIRST、LAST 及 TEXT 前六个字符；字符取值是快照还是对原数组的引用，修改 TEXT 后 FIRST 是否改变？")


@topic("FRAME字段与姿态保持")
def frame_fields(v, module):
    return Example("FRAME 的 X/Y/Z 单位为 mm，A/B/C 为角度；程序没有运动指令。", "DECL FRAME P,Q", f"""
        P={{X {400+v}.0,Y {v-10}.0,Z 300.0,A {v*3}.0,B 20.0,C 0.0}}
        Q=P
        Q.X=Q.X+{10+v}.0
        Q.Z=Q.Z-{2+v%5}.0
        P.A=P.A+5.0
    """, "列出 P、Q 的最终六个分量；这段代码是否使机器人移动，为什么只改 Q.X 不能等同于沿工具 X 轴的相对运动？")


@topic("FRAME冒号运算与坐标变换顺序")
def frame_transform(v, module):
    angle=(0,90,180,-90)[v%4]
    return Example("仅做 FRAME 计算；A 为绕 Z 轴的角度，B=C=0。不要把冒号运算视为逐字段相加。", "DECL FRAME BASE_F,OFFSET_F,R1,R2", f"""
        BASE_F={{X {100+v}.0,Y 20.0,Z 0.0,A {angle}.0,B 0.0,C 0.0}}
        OFFSET_F={{X {10+v}.0,Y 0.0,Z 0.0,A 0.0,B 0.0,C 0.0}}
        R1=BASE_F:OFFSET_F
        R2=OFFSET_F:BASE_F
    """, "解释 R1、R2 分别把平移放在哪个坐标系中，计算其 X/Y；哪些给定角度下结果相同，为什么一般不能交换冒号两侧？")


@topic("AXIS结构与关节目标")
def axis_copy(v, module):
    return Example("AXIS 只描述六个机器人关节，角度单位为度；无运动指令。", "DECL AXIS A,Q", f"""
        A={{A1 {v}.0,A2 -90.0,A3 90.0,A4 0.0,A5 20.0,A6 0.0}}
        Q=A
        Q.A1=Q.A1+{5+v}.0
        Q.A6=Q.A1-Q.A5
        A.A2=-80.0
    """, "给出 A、Q 的不同字段；AXIS 与 FRAME 的分量语义有什么区别，能否由这些赋值直接算出 TCP 的 X/Y/Z，尚缺哪些机器人信息？")


MOTION_ASSUMPTIONS = (
    "静态阅读 KSS 8.3 机器人解释器程序。假设 BAS 和标准系统变量可用，"
    "TOOL_DATA[1]、BASE_DATA[1] 已正确标定，所有给定点已在仿真中确认可达且路径无碰撞；"
    "无未声明的其他程序写入这些变量。数值用于理解，不是现场示教数据。"
)
MOTION_INIT = """BAS(#INITMOV,0)
$TOOL=TOOL_DATA[1]
$BASE=BASE_DATA[1]
$VEL.CP=0.2
$ACC.CP=0.5
$ADVANCE=0
PTP {A1 0.0,A2 -90.0,A3 90.0,A4 0.0,A5 0.0,A6 0.0}"""


@topic("PTP关节路径与最终目标")
def joint_motion(v, module):
    return Example(MOTION_ASSUMPTIONS, "DECL AXIS P1,P2", f"""P1={{A1 {v+5}.0,A2 -80.0,A3 95.0,A4 0.0,A5 10.0,A6 0.0}}
P2=P1
P2.A1=P2.A1+{10+v}.0
{MOTION_INIT}
PTP P1
PTP P2
""", "按顺序列出三个 PTP 的关节目标；PTP P1 到 PTP P2 能否保证 TCP 为直线，程序中是否给出了笛卡尔姿态和路径上的障碍物信息？")


@topic("PTP与LIN混合轨迹")
def point_linear(v, module):
    return Example(MOTION_ASSUMPTIONS, "DECL FRAME P1,P2,P3", f"""P1={{X {400+v}.0,Y 0.0,Z 400.0,A 0.0,B 90.0,C 0.0}}
P2=P1
P2.X=P2.X+{80+v}.0
P3=P2
P3.Y=60.0
{MOTION_INIT}
PTP P1
LIN P2
LIN P3
""", "说明每一段的运动类型、目标和已知直线长度；PTP P1 与 LIN P2 在路径约束上有什么区别，能否只用 $VEL.CP 计算全部运行时间？")


@topic("LIN_REL与基坐标相对位移")
def relative_motion(v, module):
    return Example(MOTION_ASSUMPTIONS+"相对运动均显式使用 #BASE；忽略机械误差，分析程序几何目标。", "DECL FRAME P", f"""P={{X 400.0,Y {v}.0,Z 400.0,A 0.0,B 90.0,C 0.0}}
{MOTION_INIT}
LIN P
LIN_REL {{X {10+v}.0}} #BASE
LIN_REL {{Y {5+v}.0}} #BASE
LIN_REL {{X -{10+v}.0}} #BASE
""", "从 P 起追踪三次相对运动后的 TCP X/Y/Z；为什么最后一次反向 X 不会消除 Y 偏移，#BASE 与工具坐标相对位移在解释上有什么区别？")


@topic("CIRC辅助点与终点")
def circular_motion(v, module):
    radius=20+v
    return Example(MOTION_ASSUMPTIONS+"三个圆弧位置共面且不共线；姿态保持相同。", "DECL FRAME START_P,AUX_P,END_P", f"""START_P={{X {400+radius}.0,Y 0.0,Z 400.0,A 0.0,B 90.0,C 0.0}}
AUX_P={{X 400.0,Y {radius}.0,Z 400.0,A 0.0,B 90.0,C 0.0}}
END_P={{X {400-radius}.0,Y 0.0,Z 400.0,A 0.0,B 90.0,C 0.0}}
{MOTION_INIT}
LIN START_P
CIRC AUX_P,END_P
""", "识别圆弧起点、辅助点和终点，说明圆所在平面和该段扫过的路径；CIRC 的第一个实参为何不是第二段独立运动的终点？")


@topic("C_DIS逼近与预进停止")
def approximation(v, module):
    return Example(MOTION_ASSUMPTIONS+"按连续自动执行分析，非单步；$ADVANCE 在代码中改为 3。", "DECL FRAME P1,P2,P3", f"""P1={{X 400.0,Y 0.0,Z 400.0,A 0.0,B 90.0,C 0.0}}
P2=P1
P2.X=P2.X+{60+v}.0
P3=P2
P3.Y={50+v}.0
{MOTION_INIT}
$ADVANCE=3
$APO.CDIS={5+v%10}.0
LIN P1
LIN P2 C_DIS
WAIT SEC 0
LIN P3
""", "解释 C_DIS 的意图、WAIT SEC 0 对预进和逼近的影响；是否能仅凭 C_DIS 认定 TCP 精确通过 P2 或肯定完成圆滑过渡？去掉 WAIT SEC 0 有何条件差异？")


@topic("CONTINUE与普通输出的执行时刻")
def advance_io(v, module):
    channel=100+v
    return Example(MOTION_ASSUMPTIONS+"连续自动执行且无外部写 $OUT；只要求相对时序，不要求毫秒时刻。", "DECL FRAME P1,P2,P3", f"""P1={{X 400.0,Y 0.0,Z 400.0,A 0.0,B 90.0,C 0.0}}
P2=P1
P2.X={470+v}.0
P3=P2
P3.Y=60.0
{MOTION_INIT}
$OUT[{channel}]=FALSE
$ADVANCE=3
LIN P1
LIN P2 C_DIS
CONTINUE
$OUT[{channel}]=TRUE
LIN P3
WAIT SEC 0
$OUT[{channel}]=FALSE
""", f"分析 $OUT[{channel}] 置 TRUE 是否必定发生在 TCP 到达 P2 时；CONTINUE 影响哪条语句，最后的 WAIT SEC 0 提供什么同步边界？这里能否代替路径触发器？")


@topic("BASE切换与同一数值目标")
def base_switch(v, module):
    return Example(MOTION_ASSUMPTIONS+f"另外 TOOL_DATA[1] 有效；BASE_DATA[2] 相对世界坐标只有 X={100+v} mm 的平移，BASE_DATA[1] 为世界零 FRAME。", "DECL FRAME TARGET", f"""TARGET={{X 400.0,Y 0.0,Z 400.0,A 0.0,B 90.0,C 0.0}}
{MOTION_INIT}
LIN TARGET
$BASE=BASE_DATA[2]
LIN TARGET
$BASE=BASE_DATA[1]
""", "两次 LIN TARGET 是否表示同一个世界空间目标？给出位置变化及最后恢复 $BASE 的作用；单独赋值 $BASE 会不会立即使机器人运动？")


@topic("阵列点生成与蛇形顺序")
def pallet(v, module):
    return Example(MOTION_ASSUMPTIONS+"只生成并记录目标坐标，没有逐点运动；TRACE_X、TRACE_Y 保存最后一次生成的点。", "DECL INT R,C\nDECL REAL TRACE_X,TRACE_Y\nDECL FRAME ORIGIN,TARGET", f"""
        ORIGIN={{X {400+v}.0,Y 100.0,Z 300.0,A 0.0,B 90.0,C 0.0}}
        TARGET=ORIGIN
        FOR R=1 TO 2
          FOR C=1 TO 3
            TARGET.Y=ORIGIN.Y+(R-1)*{30+v}.0
            IF R==1 THEN
              TARGET.X=ORIGIN.X+(C-1)*{40+v}.0
            ELSE
              TARGET.X=ORIGIN.X+(3-C)*{40+v}.0
            ENDIF
            TRACE_X=TARGET.X
            TRACE_Y=TARGET.Y
          ENDFOR
        ENDFOR
    """, "按循环顺序列出六个 TARGET 的 X/Y，给出最后 TRACE_X、TRACE_Y；为何第二行方向反转，是否修改了 ORIGIN 或姿态分量？")


@topic("E6POS与外部轴分量")
def external_axis(v, module):
    return Example(MOTION_ASSUMPTIONS+"本题假设 E1 是已配置的非耦合直线外部轴，单位 mm；其余外部轴未参与工艺。", "DECL E6POS P1,P2", f"""P1={{X 400.0,Y 0.0,Z 400.0,A 0.0,B 90.0,C 0.0,S 6,T 35,E1 {v*5}.0,E2 0.0,E3 0.0,E4 0.0,E5 0.0,E6 0.0}}
P2=P1
P2.E1=P2.E1+{20+v}.0
P2.X=P2.X+10.0
{MOTION_INIT}
PTP P1
PTP P2
""", "列出 P1、P2 的 X 与 E1 差值，区分 S/T、A/B/C、E1 的作用；为何不能仅凭两条 PTP 断定机器人与外部轴构成给定速度的协调直线？")


@topic("CP速度赋值与运动段作用域")
def velocity(v, module):
    return Example(MOTION_ASSUMPTIONS+"三次 LIN 均精确定位且连续自动执行；倍率、加减速及控制器约束未知。", "DECL FRAME P1,P2,P3", f"""P1={{X 400.0,Y 0.0,Z 400.0,A 0.0,B 90.0,C 0.0}}
P2=P1
P2.X=500.0
P3=P2
P3.Y=100.0
{MOTION_INIT}
$VEL.CP={0.10+v/100:.2f}
LIN P1
$VEL.CP={0.20+v/100:.2f}
LIN P2
$VEL.CP={0.05+v/100:.2f}
LIN P3
""", "为每段 LIN 标注当时 $VEL.CP 值及单位，解释其与 PTP 关节速度设置的区别；为什么把距离除以该值只能得到理想化估计，不能保证实测节拍？")


@topic("电平握手与等待释放")
def handshake(v, module):
    inp=10+v
    out=40+v
    return Example(f"$IN[{inp}] 初始 FALSE；PLC 在请求 $OUT[{out}] 为 TRUE 后才置输入 TRUE，看到请求清零后再清输入。输入变化时间未给出，普通 I/O 均不是安全信号。", "", f"""
        $OUT[{out}]=FALSE
        WAIT FOR NOT $IN[{inp}]
        $OUT[{out}]=TRUE
        WAIT FOR $IN[{inp}]
        $OUT[{out}]=FALSE
        WAIT FOR NOT $IN[{inp}]
        $OUT[{out+40}]=TRUE
    """, "按 PLC 响应列出握手阶段和每个 WAIT FOR 的放行条件；PLC 永远不置输入或永远不清输入时，各停在哪里，程序有没有超时恢复？")


@topic("毫秒计时器与等待超时")
def timeout(v, module):
    inp=50+v
    threshold=500+v*100
    return Example(f"计时器 1 无其他使用者，$IN[{inp}] 初始 FALSE 且等待期间可能变 TRUE；计时器和 I/O 有正常扫描延迟，不假设恰在阈值处采样。", "DECL BOOL TIMED_OUT", f"""
        $TIMER_STOP[1]=TRUE
        $TIMER[1]=0
        $TIMER_STOP[1]=FALSE
        WAIT FOR $IN[{inp}] OR ($TIMER[1]>{threshold})
        $TIMER_STOP[1]=TRUE
        TIMED_OUT=NOT $IN[{inp}]
        $OUT[{inp}]=NOT TIMED_OUT
    """, f"解释 $TIMER_STOP 的 TRUE/FALSE 和 {threshold} 的单位；分别分析输入先到、一直不到、超时放行后输入又在赋值前到达的情况，TIMED_OUT 是否锁存了首次放行原因？")


@topic("采样上升沿与短脉冲丢失")
def rising_edge(v, module):
    return Example(f"输入轨迹按每次赋值 CUR=$IN[{80+v}] 的采样时刻给出：FALSE、TRUE、TRUE、FALSE、TRUE、FALSE。期间可能存在未采到的短脉冲；不分析循环索引结束值。", "DECL INT I,COUNT\nDECL BOOL PREV,CUR", f"""
        COUNT={v}
        PREV=FALSE
        FOR I=1 TO 6
          CUR=$IN[{80+v}]
          IF CUR AND NOT PREV THEN
            COUNT=COUNT+1
          ENDIF
          PREV=CUR
          WAIT SEC 0.05
        ENDFOR
    """, "逐次列出 CUR、PREV 更新及 COUNT；为何持续 TRUE 不会每次加一、轮询期间短脉冲可能丢失，以及该写法与直接 IF CUR 的差别。")


@topic("SIGNAL组合字与位序")
def signal_word(v, module):
    value=v+1
    return Example(f"配套 DAT 中 SIGNAL 将 $IN[101] 至 $IN[104] 组合为无符号输入字；本次输入字值为 {value%16}，四个位在读取期间稳定，$OUT[101..104] 只由本程序写。", "DECL INT CODE", f"""
        CODE=JOB_BITS
        ACK_BITS=0
        IF CODE>={1+v%8+v//8} THEN
          ACK_BITS=CODE
        ELSE
          ACK_BITS=15
        ENDIF
    """, "根据给定输入列出 CODE、ACK_BITS 及四个输出位；SIGNAL 声明中的起始位如何对应最低有效位，四位可表达的范围是什么？", data="SIGNAL JOB_BITS $IN[101] TO $IN[104]\nSIGNAL ACK_BITS $OUT[101] TO $OUT[104]")


@topic("PULSE与非阻塞脉冲")
def pulse(v, module):
    channel=150+v
    duration=0.2+v*0.01
    return Example(f"$OUT[{channel}] 初始 FALSE，只有当前程序控制该输出；分析正常连续执行且未中断、未复位的情况，具体扫描时延未知。", "DECL INT TRACE", f"""
        TRACE=0
        PULSE($OUT[{channel}],TRUE,{duration:.2f})
        TRACE=1
        WAIT SEC {duration+0.3:.2f}
        TRACE=2
    """, "说明脉冲期望持续时间及单位、TRACE=1 是否等待脉冲结束才执行；WAIT SEC 后预期输出状态是什么，如何区分 PULSE 自身与显式等待？")


@topic("模拟量缩放与限幅")
def analog(v, module):
    raw=(v-10)/10
    gain=1.2+v/20
    return Example(f"在读取瞬间 $ANIN[1]={raw:.1f}，为已配置模拟通道的归一化 REAL 值；这里的 GAIN 不代表任何设备量纲，$ANOUT[1] 无其他写入者。", "DECL REAL RAW,CMD,GAIN", f"""
        GAIN={gain:.2f}
        RAW=$ANIN[1]
        CMD=RAW*GAIN
        IF CMD>1.0 THEN
          CMD=1.0
        ENDIF
        IF CMD<0.0 THEN
          CMD=0.0
        ENDIF
        $ANOUT[1]=CMD
    """, "逐步计算 RAW、限幅前 CMD、最终输出；为何负输入被截到 0 而不是 -1，且不能仅靠此代码推断电压或实际执行器流量？")


@topic("延时复采样与去抖边界")
def debounce(v, module):
    inp=180+v
    delay=0.03+v*0.005
    return Example(f"$IN[{inp}] 在 WAIT FOR 时最初 FALSE，随后变 TRUE 使等待放行。需要分别考虑它在 WAIT SEC 期间保持 TRUE、先变 FALSE 再变 TRUE、最终变 FALSE 三种轨迹；无其他程序写输出。", "DECL BOOL ACCEPTED", f"""
        ACCEPTED=FALSE
        WAIT FOR $IN[{inp}]
        WAIT SEC {delay:.3f}
        IF $IN[{inp}] THEN
          ACCEPTED=TRUE
        ENDIF
        $OUT[{inp}]=ACCEPTED
    """, "分别说明三种轨迹的 ACCEPTED；这段程序是否证明输入在整个延时窗口连续为 TRUE，还是只观察了两个时刻？失败后是否自动重新等待？")


@topic("计时测量与停止后读数")
def timer_measurement(v, module):
    wait=0.05+v*0.01
    return Example("计时器 2 独占，按正常执行分析；时钟粒度、解释器调度与执行开销未知，不把 WAIT SEC 当作精确实时调度。", "DECL INT ELAPSED,AFTER_WAIT", f"""
        $TIMER_STOP[2]=TRUE
        $TIMER[2]=0
        $TIMER_STOP[2]=FALSE
        WAIT SEC {wait:.2f}
        $TIMER_STOP[2]=TRUE
        ELAPSED=$TIMER[2]
        WAIT SEC 0.20
        AFTER_WAIT=$TIMER[2]
    """, "ELAPSED 的单位是什么、理想等待贡献是多少毫秒？停止后第二次等待是否应增加计时器，为什么不能承诺 ELAPSED 恰为理想值？")


@topic("中断使能与共享计数")
def interrupt(v, module):
    channel=220+v
    helper=f"EDGE_{module[-4:]}"
    return Example(f"中断号 10 未被占用；$IN[{channel}] 在 INTERRUPT ON 10 时为 FALSE，然后在等待期间仅出现一次持续足够长的 FALSE→TRUE，随后输入 {channel+30} 才变 TRUE 结束等待。无其他写入 COUNT 的程序；本中断不承担安全功能。", "", f"""
        INTERRUPT DECL 10 WHEN $IN[{channel}]==TRUE DO {helper}()
        COUNT={v}
        INTERRUPT ON 10
        WAIT FOR $IN[{channel+30}]
        INTERRUPT OFF 10
        LAST_COUNT=COUNT
    """, "解释 DECL、ON、OFF 各自作用及中断过程如何影响 COUNT、LAST_COUNT；输入一直维持 TRUE 是否意味着每个解释器周期都重复调用，代码中有没有 BRAKE 或 RESUME？", extra_src=f"""
        DEF {helper}()
          COUNT=COUNT+1
        END
    """, data="DECL INT COUNT=0\nDECL INT LAST_COUNT=0")


@topic("路径触发与端点切换")
def trigger(v, module):
    channel=260+v
    return Example(MOTION_ASSUMPTIONS+f"$OUT[{channel}] 由本程序独占；运动按连续自动执行，触发器可正常执行。", "DECL FRAME P1,P2", f"""P1={{X 400.0,Y 0.0,Z 400.0,A 0.0,B 90.0,C 0.0}}
P2=P1
P2.X={500+v}.0
{MOTION_INIT}
$OUT[{channel}]=FALSE
$ADVANCE=3
LIN P1
TRIGGER WHEN DISTANCE=1 DELAY=0 DO $OUT[{channel}]=TRUE
LIN P2
WAIT SEC 0.10
$OUT[{channel}]=FALSE
""", f"说明 TRIGGER 关联哪条运动、DISTANCE=1 与 DELAY=0 的含义；$OUT[{channel}] 在何种路径事件切换，为什么 DISTANCE=1 不能解释成距起点 1 mm？")


LENSES = (
    ("执行追踪", "请按实际执行顺序分析，给出关键中间状态和最终结果；涉及外部时序时分情况列出，不虚构确定时间。"),
    ("边界推演", "请先解释当前样本，再围绕题目指定的边界或替代输入逐项比较，指出改变结果的具体语句。"),
    ("语义辨析", "请结合该程序区分容易混淆的 KRL 语义，引用具体语句说明；信息不足的结论请明确指出所缺条件。"),
    ("评审说明", "请像评审已有程序一样解释数据、流程和可验证的行为；列出能检查这些结论的观测点，不要求生成另一套完整程序。"),
)


def render_question(number, title, build, variant):
    module = f"KRLU_{number:04d}"
    example = build(variant, module)
    lens, lens_text = LENSES[variant % len(LENSES)]
    src_lines = [f"; FILE: {module}.src", f"DEF {module}()"]
    for part in (example.declarations, example.body):
        if part.strip():
            src_lines.extend("  " + line if line else "" for line in clean(part).splitlines())
    src_lines.append("END")
    if example.extra_src.strip():
        src_lines.extend(["", clean(example.extra_src)])
    blocks = ["```krl\n" + "\n".join(src_lines) + "\n```"]
    if example.data.strip():
        blocks.append(
            f"```krl\n; FILE: {module}.dat\nDEFDAT {module}\n"
            + "\n".join("  " + line for line in clean(example.data).splitlines())
            + "\nENDDAT\n```"
        )
    return (
        f"## {number:04d} {title}｜案例{variant+1:02d}｜{lens}\n\n"
        f"【样本{number:04d}｜KRL程序理解｜{title}｜{lens}】请阅读下面生成的 KRL 样例，解释已有程序。\n\n"
        f"运行前提：{example.assumptions}\n\n"
        + "\n\n".join(blocks) +
        f"\n\n理解问题：{example.questions}\n\n回答要求：{lens_text}\n"
    )


def render():
    if len(TOPICS) != 50:
        raise ValueError(f"Expected 50 program families, found {len(TOPICS)}")
    lines = [
        "# KRLU程序理解问题采样清单（1000题）",
        "采样矩阵：50种程序语义主题 × 20个具体样例，共1000题；每主题包含执行追踪、边界推演、语义辨析、评审说明四种提问视角。每题附独立生成的 SRC，必要时附同名 DAT。",
        "适用入口：KUKA Center 小库的「KRL程序理解」模式。先将该题所有带 FILE 标记的代码块分别作为同名 .src/.dat 文件上传并完成程序分析，再发送完整问题；不要把其他题目的程序上下文混入本题。",
        "这是一份提问清单，不含参考答案。样例面向 KUKA KSS 8.3 的基础 KRL 语义，供静态阅读与聊天机器人问答采样；尚未经 KUKA 控制器编译或实际运行。机器人型号、外部轴配置、现场 I/O 和真实设备行为不能从示例数值推定。涉及运动的题目在题内列出虚拟环境前提，普通工艺 I/O 不代表安全功能。",
        "语法范围参照 KUKA 编著的 [KUKA System Software 8.3 SI V4（2015-01-14），第11章](https://www.mroelectric.com/static/app/product/pdfs/00-291-556.pdf)；样例与问题均为新生成内容。",
        "使用 `python utils/krl/generate_krlu_questions.py --check` 可检查已提交题库是否与确定性生成结果一致。已有采集输出依赖完整 prompt 的校验值，开始采集后修改题库需要核对来源兼容性。",
        "主题索引（每主题20题）：\n\n" + "\n".join(
            f"- {i*20+1:04d}–{(i+1)*20:04d}：{title}"
            for i, (title, _) in enumerate(TOPICS)
        ),
    ]
    for index, (title, build) in enumerate(TOPICS):
        for variant in range(20):
            lines.append(render_question(index * 20 + variant + 1, title, build, variant))
    return "\n\n".join(lines).rstrip() + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true", help="compare without writing any files")
    args = parser.parse_args(argv)
    content = render()
    if args.check:
        if not args.output.is_file() or args.output.read_text(encoding="utf-8") != content:
            parser.exit(1, f"Question bank differs from generated content: {args.output}\n")
        print(f"Verified 1000 generated questions: {args.output}")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8")
        print(f"Wrote 1000 generated questions: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
