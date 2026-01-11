grammar Job;
// ****************************
// Last Modified :2025.9.10
// ********** RC 5.0 **********
//options { caseInsensitive = true; } 
//tokens { NUMBER }


// *********************************
// ********** lexer rules **********
STR2I : 'STR2I';
STR2R : 'STR2R';
I2STR : 'I2STR';
R2STR : 'R2STR';
NOP : 'NOP';
END : 'END';
MOVJ : 'MOVJ';
MOVL : 'MOVL';
MOVS : 'MOVS';
MOVC : 'MOVC';
ACC : 'ACC';
CNT : 'CNT';
FINE : 'FINE';
V : 'V';
P : 'P';
PR : 'PR';
//WS: [' ' | '\t']+ -> skip;
ASSIGN: '=';
SET : 'SET';
TYPE : 'TYPE';
I : 'I';
H : 'H';
B : 'B';
R : 'R';
S : 'S';
SI : 'SI';
SH : 'SH';
SB : 'SB';
SR : 'SR';
X : 'x';
Y : 'y';
Z : 'z';
//RX : 'rx';
//RY : 'ry';
RZ : 'rz';
J1 : 'j1';
J2 : 'j2';
J3 : 'j3';
J4 : 'j4';
SETP : 'SETP';
CFG : 'CFG';
FUT : 'FUT';
NUT : 'NUT';
FDT : 'FDT';
FUB : 'FUB';
NDT : 'NDT';
FDB : 'FDB';
NUB : 'NUB';
NDB : 'NDB';
TN : 'TN';
TF : 'TF';
UF : 'UF';
//ET : 'ET';
GETP : 'GETP';
SETEXTAXIS : 'SETEXTAXIS';
CLEAR : 'CLEAR';
STRLEN : 'STRLEN';
STRFIND : 'STRFIND';
STRSUB : 'STRSUB';
STRCMP : 'STRCMP';
FRAME : 'FRAME';
NOT : 'NOT';
ATAN : 'ATAN';
INV : 'INV';
SQRT : 'SQRT';
SIN : 'SIN';
COS : 'COS';
TAN : 'TAN';
ATAN2 : 'ATAN2';
MAX : 'MAX';
MIN : 'MIN';
ABS : 'ABS';
LOG : 'LOG';
POW : 'POW';
IN : 'IN';
OUT : 'OUT';
AINT : 'AINT';
AOUT : 'AOUT';
DI : 'DI';
GIB : 'GIB';
GIH : 'GIH';
GII : 'GII';
GIF : 'GIF';
DO : 'DO';
GOB : 'GOB';
GOH : 'GOH';
GOI : 'GOI';
GOF : 'GOF';
AI : 'AI';
AO : 'AO';
ANIN : 'ANIN';
ANOUT : 'ANOUT';
MACRO : 'MACRO';
IF : 'IF';
ENDIF : 'ENDIF';
ELSE : 'ELSE';
ELSEIF : 'ELSEIF';
WHILE : 'WHILE';
ENDWHILE : 'ENDWHILE';
LABEL : 'LABEL';
GOTO : 'GOTO';
CALL : 'CALL';
WAIT : 'WAIT';
DELAY : 'DELAY';
STARTBG : 'START_BG';
STOPBG : 'STOP_BG';
UALM : 'UALM';
IRQON : 'IRQON';
IRQOFF : 'IRQOFF';
TIMERSTART : 'TIMER START';
TIMERSTOP : 'TIMER STOP';
USER_OFFSET_CONDITION : 'USER_OFFSET_CONDITION';
TOOL_OFFSET_CONDITION : 'TOOL_OFFSET_CONDITION';
USER_OFFSET : 'USER_OFFSET';
TOOL_OFFSET : 'TOOL_OFFSET';
T : 'T';
AND : 'AND';
OR : 'OR';
LEV : 'LEV';
INDEX : 'INDEX';
CONDITION : 'CONDITION';
GETAT : 'GETAT';
STRFINDEND : 'STRFINDEND';
STRREVERSE : 'STRREVERSE';
STRSPLIT : 'STRSPLIT';
SPRINTF : 'SPRINTF';
SWITCH : 'SWITCH';
CASE : 'CASE';
DEFAULT : 'DEFAULT';
ENDSWITCH : 'ENDSWITCH';
BREAK : 'BREAK';
CONTINUE : 'CONTINUE';
//PRINTF : 'PRINTF';
JUMP : 'JUMP';
JUMPL : 'JUMPL';
ARMCHANGE : 'ARMCHANGE';
RECORDMOTIONSTART : 'RECORDMOTIONSTART';
RECORDMOTIONSTOP : 'RECORDMOTIONSTOP';
LEFT : 'LEFT';
RIGHT : 'RIGHT';
AUTO : 'AUTO';
F : 'F';
LH : 'LH';
MH : 'MH';
RH : 'RH';
SETFRAME : 'SETFRAME';
TCPCREATE : 'TCPCREATE';
TCPSEND : 'TCPSEND';
TCPRECV : 'TCPRECV';
TCPCLOSE : 'TCPCLOSE';
SERVERIP : 'SERVERIP';
PORT : 'PORT';
TIMEOUT : 'TIMEOUT';
DIS : 'DIS';
CODE : 'CODE';
FOR : 'FOR';
ENDFOR : 'ENDFOR';
STEP : 'STEP';
PAUSE : 'PAUSE';
NEWLINE : '\r'? '\n';
//COMMENT : '// ' .*? '\r'? '\n' -> channel(HIDDEN);
COMMENT : '// ' .*? '\r'? '\n';
JOBSKIP : 'SKIP';
//NOTE : '! ' .*? '\r'? '\n' -> channel(HIDDEN);
NOTE : '! ' .*? '\r'? '\n';
//INT : [1-9]DIGIT*;
INT : '0' | [1-9]DIGIT*;
FLOAT : DIGIT+ '.' DIGIT+;
NOEQU : '!=';
BITOR : '^';
OP_AND : '&&';
OP_OR : '||';
AMP : '&';
BITWISE_OR : '|';
OPEN_PARENS : '(';
CLOSE_PARENS : ')';
ID : [a-zA-Z0-9_.]+;
fragment DIGIT:[0-9];
STRING : '"' .*? '"';
SPACE : ' ' | '\t';

// **********************************
// ********** parser rules **********
prog : instStart (inst)* instEnd;
//inst : (copInst | ioInst | baseInst | moveInst | controlInst | operateInst | NOTE | COMMENT) NEWLINE;
inst : (opExp NEWLINE) | NOTE | COMMENT;
instStart : NOP NEWLINE;
instEnd : END EOF;

// ********** 运算指令规则 **********
copInst : INV SPACE positionVar
| intVar assignOp NOT SPACE (number | numVar | positionVarScalar)
| floatVar assignOp (ATAN | SQRT) SPACE (number | numVar | positionVarScalar)
| floatVar assignOp (SIN | COS | TAN) SPACE (number | numVar | positionVarScalar)
| floatVar assignOp ATAN2 SPACE (number | numVar | positionVarScalar) SPACE (number | numVar | positionVarScalar)
| (numVar | positionVarCoordScalar) assignOp (MAX | MIN) SPACE (numVar | number | positionVarScalar) SPACE (numVar | number | positionVarScalar)
| numVar assignOp (LOG | POW) SPACE (numVar | number | positionVarScalar) SPACE (numVar | number | positionVarScalar)
| numVar assignOp ABS SPACE (numVar | number | positionVarScalar)
| operateExp
;

operateExp : numScalarOpExp
| positionVar assignOp positionVar (SPACE '+' SPACE positionVar)*
| strVar assignOp strVar (SPACE '+' SPACE strVar)*
;
//| strVar assignOp (STRING|strVar) (SPACE '+' SPACE (STRING|strVar))*

numScalarOpExp :(numVar | positionVarCoordScalar) assignOp copOpExp
;
copOpExp :(OPEN_PARENS SPACE)? (number | numVar | positionVarCoordScalar) ( SPACE ('+' | '-' | '*' | '/' | OP_AND | OP_OR| BITWISE_OR | AMP | '<<' | '>>' | BITOR) SPACE copOpExp)* (SPACE CLOSE_PARENS)?  //todo:需后台补充判定操作右侧的数据类型
;
assignOp : SPACE ASSIGN SPACE
;
varIndex : INT | I'[' INT ']'
;


// ********** 基础指令规则 **********
baseInst : setInst
| setPInst
| GETP SPACE positionVar
| setExtAxisInst
| clearInst
| STRLEN SPACE strVar SPACE iVar
| STRFIND SPACE strVar SPACE strVar SPACE iVar
| STRSUB SPACE strVar SPACE iVar SPACE iVar SPACE strVar
| STRCMP SPACE strVar SPACE strVar SPACE iVar
| GETAT SPACE strVar SPACE iVar SPACE strVar
| STRFINDEND SPACE strVar SPACE strVar SPACE iVar
| STRREVERSE SPACE strVar SPACE strVar
| STRSPLIT SPACE strVar SPACE strVar SPACE INT SPACE iVar
| SPRINTF (SPACE strVar)+
| setFrameInst
| str2iInst
| str2rInst
| i2strInst
| r2strInst
;

clearInst : CLEAR SPACE iVar SPACE iVar
| CLEAR SPACE hVar SPACE hVar
| CLEAR SPACE bVar SPACE bVar
| CLEAR SPACE floatVar SPACE floatVar
; 
setInst : SET SPACE positionVar SPACE positionVar 
| SET SPACE (numVar | positionVarScalar) SPACE (positionVarScalar | numVar | number)
| SET SPACE strVar SPACE strVar
;
intVar : iVar | hVar | bVar;
floatVar : R'['varIndex']';  //64位浮点型变量
numVar : intVar | floatVar;
strVar : S'['varIndex']';  //字符型变量
positionVarScalar : positionVarCoordScalar | positionVarJointScalar;
positionVarCoordScalar : (P|PR)'['varIndex'].'(X|Y|Z|RZ);   //暂时只支持4轴
positionVarJointScalar : (P|PR)'['varIndex'].'(J1|J2|J3|J4);     //暂时只支持4轴
iVar : I'['varIndex']';  //32位整型变量
hVar : H'['varIndex']';  //16位整型变量
bVar : B'['varIndex']';  //8位整型变量
setExtAxisInst : SETEXTAXIS SPACE positionVar SPACE ID SPACE (number | numVar)     // ID 范围e1-e6
;

setPInst : SETP SPACE positionVar SPACE FRAME SPACE (TF|UF) SPACE (number|intVar)
;
// | SETP SPACE positionVar SPACE CFG SPACE (FUT|NUT|FDT|FUB|NDT|FDB|NUB|NDB)
// | SETP SPACE positionVar SPACE tnValue SPACE number  //  不能写 ('1'|'4'|'6') ，否则出现 1不能识别成INT的报错
// SETP SPACE positionVar SPACE FRAME SPACE (TF|UF|ET) SPACE (number|numVar)
number : ('-')? (INT|FLOAT); 
tnValue : TN SPACE INT;  // INT 范围1、4、6
setFrameInst : SETFRAME SPACE (TF|UF) SPACE (INT|intVar);
//| SETFRAME SPACE (ET|TF|UF) SPACE (INT|intVar)
str2iInst : STR2I SPACE strVar SPACE iVar;
str2rInst : STR2R SPACE strVar SPACE floatVar;
i2strInst : I2STR SPACE iVar SPACE strVar;
r2strInst : R2STR SPACE floatVar SPACE strVar;

// ********** IO指令规则 **********
ioInst : IN SPACE (iVar | floatVar) assignOp digitalIOVar
| outExp
| ANIN SPACE floatVar assignOp anInVar
| ANOUT SPACE anOutVar assignOp (positionVarScalar | numVar | number)
;

digitalIOVar : (diVar | giBVar | giHVar | giIVar | giFVar | doVar | goBVar | goHVar | goIVar | goFVar)
;
outExp : OUT SPACE (doVar|goBVar|goHVar|goIVar|goFVar) SPACE (number | numVar) ( SPACE tDIS assignOp (number| intVar))?
;
tDIS : T | DIS
;
anInVar : AI'['varIndex']'
;
anOutVar : AO '['varIndex']'
;
diVar : DI '['varIndex']'
;
giBVar : GIB '['varIndex']'
;
giHVar : GIH '['varIndex']'
;
giIVar : GII '['varIndex']'
;
giFVar : GIF '['varIndex']'
;
doVar : DO '['varIndex']'
;
goBVar : GOB '['varIndex']'
;
goHVar : GOH '['varIndex']'
;
goIVar : GOI '['varIndex']'
;
goFVar : GOF '['varIndex']'
;


// ********** 控制指令规则 **********
controlInst : ifExp
| whileExp
| labelExp
| gotoExp
| delayExp
| waitExp
| callExp
| paracallExp
| UALM SPACE CODE assignOp (INT |intVar)
| switchExp
| forExp
| PAUSE
;
//MACRO SPACE marcoFile  暂时不需要
marcoFile : ID;
jobOrBackgroundFile : ID;
ifExp : IF SPACE multiConExp SPACE ':' SPACE (gotoExp | callExp | outExp | delayExp | waitExp)
| IF SPACE multiConExp NEWLINE (inst)+ (ELSE NEWLINE (inst)+)? ENDIF
| IF SPACE multiConExp NEWLINE (inst)+ (ELSEIF SPACE multiConExp NEWLINE (inst)+)* ENDIF
;
opExp : copInst | ioInst | baseInst | moveInst | operateInst | controlInst
;
whileExp : WHILE SPACE ((INT | intVar | multiConExp) NEWLINE) (whileOpExp)* ENDWHILE
;
whileOpExp : inst | ((CONTINUE | BREAK) NEWLINE)//todo:后续需补充和筛选
;
multiConExp : conditionExp ( SPACE AND SPACE conditionExp)*
| conditionExp ( SPACE OR SPACE conditionExp)*
;
conditionExp : ( number | numVar | positionVarScalar | digitalIOVar) relationOp (number |
 numVar | positionVarScalar)
;
delayExp : DELAY SPACE T assignOp (number | numVar)
;
gotoExp : GOTO SPACE STRING
;
callExp : CALL SPACE jobOrBackgroundFile
;
waitExp : WAIT SPACE digitalIOVar assignOp (number | intVar) SPACE T assignOp (number | intVar)
;
relationOp :SPACE ('==' | NOEQU | '>' | '>=' | '<' | '<=') SPACE
;
switchExp : SWITCH SPACE (numVar | strVar) NEWLINE (NOTE|COMMENT)* caseExp* (NOTE|COMMENT)* DEFAULT NEWLINE (switchOpExp)* BREAK NEWLINE (NOTE|COMMENT)* ENDSWITCH
;
switchOpExp : inst | (BREAK NEWLINE) //todo:后续需补充和筛选
;
caseExp : CASE SPACE (numVar | strVar) NEWLINE (switchOpExp)* BREAK NEWLINE;
paracallExp : ( STARTBG | STOPBG ) SPACE jobOrBackgroundFile
;
labelExp : LABEL SPACE STRING
;
forExp : FOR SPACE intVar assignOp (intVar | number) SPACE intVar SPACE ('>' | '>=' | '<' | '<=') SPACE (intVar | number) SPACE STEP SPACE ('-')? INT NEWLINE (whileOpExp)* ENDFOR
;

// ********** 运动指令规则 **********
moveInst : MOVJ SPACE positionVar SPACE velocityAssign SPACE accAssign SPACE crAssign (SPACE additionalInst)?     //限制1-100
| MOVL SPACE positionVar SPACE velocityAssign SPACE accAssign SPACE crAssign (SPACE additionalInst)?
| MOVC SPACE positionVar SPACE velocityAssign SPACE accAssign SPACE crAssign (SPACE additionalInst)?
| MOVC SPACE positionVar SPACE velocityAssign SPACE accAssign SPACE crAssign (SPACE ':' SPACE SET SPACE TYPE assignOp INT)?
| MOVS SPACE positionVar SPACE velocityAssign SPACE accAssign SPACE crAssign
| JUMP SPACE positionVar SPACE velocityAssign SPACE accAssign SPACE crAssign SPACE lhAssign SPACE mhAssign SPACE rhAssign
| JUMPL SPACE positionVar SPACE velocityAssign SPACE accAssign SPACE crAssign SPACE lhAssign SPACE mhAssign SPACE rhAssign
| ARMCHANGE SPACE (LEFT|RIGHT|AUTO) SPACE velocityAssign
;
posGVar : PR'['varIndex']';
posLVar : P'['varIndex']';
positionVar : posGVar | posLVar;
velocityAssign : V assignOp (number | numVar);
accAssign : ACC assignOp number;
cntAssign : CNT assignOp number;
crAssign : (cntAssign | FINE);
additionalInst: ':' SPACE (skipLaExp
| outExp
| TOOL_OFFSET (SPACE positionVar)?
| USER_OFFSET (SPACE positionVar)?)
;
skipLaExp : JOBSKIP SPACE STRING
;
lhAssign : LH assignOp number;   //限制 [-2000,2000]
mhAssign : MH assignOp number;   //限制 [-2000,2000]
rhAssign : RH assignOp number;   //限制 [-2000,2000]


// ********** 操作指令规则 **********
operateInst : irqOnExp
| irqOffExp
| TIMERSTART SPACE INDEX assignOp INT
| TIMERSTOP SPACE INDEX assignOp INT SPACE (iVar | floatVar)
| JOBSKIP SPACE CONDITION SPACE digitalIOVar SPACE '==' SPACE number
| USER_OFFSET_CONDITION SPACE positionVar SPACE ufValue
| TOOL_OFFSET_CONDITION SPACE positionVar SPACE tfValue
| RECORDMOTIONSTART SPACE tAssign
| RECORDMOTIONSTOP SPACE fAssign
| TCPCREATE SPACE STRING SPACE SERVERIP assignOp ID SPACE PORT assignOp (INT|intVar)
| TCPSEND SPACE STRING SPACE (strVar|number)
| TCPRECV SPACE STRING SPACE (number|strVar) SPACE (number|intVar) SPACE TIMEOUT assignOp (number|intVar) SPACE iVar
| TCPCLOSE SPACE STRING
;
//| commentExp
//| PRINTF SPACE (strVar | numVar | positionVar)

irqOnExp : IRQON SPACE levAssign SPACE (diVar|iVar) SPACE '==' SPACE number SPACE jobOrBackgroundFile
;
irqOffExp : IRQOFF SPACE levAssign
;
levAssign : LEV assignOp INT;  //INT 范围0-100
ufValue : UF'['varIndex']';    //INT 范围0-8
tfValue : TF'['varIndex']';    //INT 范围0-8
//vAssign : V assignOp number;
fAssign : F assignOp (number|intVar);
tAssign : T assignOp (number|intVar);
//commentExp : COMMENT SPACE (copInst | ioInst | baseInst | moveInst | controlInst | operateInst | NOTE);
//error : .+? ; // 匹配任意字符序列直到下一个换行符