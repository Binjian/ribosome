/**
 * @file grammar.js
 * @author Siasun Robot Language Support (converted from Job.g4)
 * @license MIT
 */

/// <reference types="tree-sitter-cli/dsl" />

module.exports = grammar({
  name: "siasun_robot",

  extras: ($) => [
    $.SPACE,
  ],

  conflicts: $ => [
    [$.copInst],
  ],

  rules: {
    // Main program structure
    prog: ($) => seq(
      $.instStart,
      repeat($.inst),
      $.instEnd
    ),

    inst: ($) => choice(
      seq($.opExp, $.NEWLINE),
      $.NOTE,
      $.COMMENT
    ),

    instStart: ($) => seq("NOP", $.NEWLINE),
    instEnd: ($) => "END",

    // Comments and notes - include newline like in Job.g4
    COMMENT: ($) => token(seq("// ", /[^\r\n]*/, /\r?\n/)),
    NOTE: ($) => token(seq("! ", /[^\r\n]*/, /\r?\n/)),
    NEWLINE: ($) => token(/\r?\n/),
    SPACE: ($) => token(choice(" ", "\t")),

    // Literals
    INT: ($) => token(choice("0", seq(/[1-9]/, repeat(/[0-9]/)))),
    FLOAT: ($) => token(seq(repeat1(/[0-9]/), ".", repeat1(/[0-9]/))),
    STRING: ($) => token(seq('"', repeat(/[^"]/), '"')),
    ID: ($) => token(/[a-zA-Z0-9_.]+/),

    number: ($) => choice(
      seq(optional("-"), $.INT),
      seq(optional("-"), $.FLOAT)
    ),

    // Variables
    varIndex: ($) => choice(
      $.INT,
      seq("I", "[", $.INT, "]")
    ),

    iVar: ($) => seq("I", "[", $.varIndex, "]"),
    hVar: ($) => seq("H", "[", $.varIndex, "]"),
    bVar: ($) => seq("B", "[", $.varIndex, "]"),
    floatVar: ($) => seq("R", "[", $.varIndex, "]"),
    strVar: ($) => seq("S", "[", $.varIndex, "]"),

    intVar: ($) => choice($.iVar, $.hVar, $.bVar),
    numVar: ($) => choice($.intVar, $.floatVar),

    posGVar: ($) => seq("PR", "[", $.varIndex, "]"),
    posLVar: ($) => seq("P", "[", $.varIndex, "]"),
    positionVar: ($) => choice($.posGVar, $.posLVar),

    positionVarCoordScalar: ($) => seq(
      choice("P", "PR"),
      "[", $.varIndex, "]",
      ".",
      choice("X", "Y", "Z", "RZ")
    ),

    positionVarJointScalar: ($) => seq(
      choice("P", "PR"),
      "[", $.varIndex, "]",
      ".",
      choice("J1", "J2", "J3", "J4")
    ),

    positionVarScalar: ($) => choice(
      $.positionVarCoordScalar,
      $.positionVarJointScalar
    ),

    // IO Variables
    diVar: ($) => seq("DI", "[", $.varIndex, "]"),
    doVar: ($) => seq("DO", "[", $.varIndex, "]"),
    giBVar: ($) => seq("GIB", "[", $.varIndex, "]"),
    giHVar: ($) => seq("GIH", "[", $.varIndex, "]"),
    giIVar: ($) => seq("GII", "[", $.varIndex, "]"),
    giFVar: ($) => seq("GIF", "[", $.varIndex, "]"),
    goBVar: ($) => seq("GOB", "[", $.varIndex, "]"),
    goHVar: ($) => seq("GOH", "[", $.varIndex, "]"),
    goIVar: ($) => seq("GOI", "[", $.varIndex, "]"),
    goFVar: ($) => seq("GOF", "[", $.varIndex, "]"),
    anInVar: ($) => seq("AI", "[", $.varIndex, "]"),
    anOutVar: ($) => seq("AO", "[", $.varIndex, "]"),

    digitalIOVar: ($) => choice(
      $.diVar,
      $.giBVar,
      $.giHVar,
      $.giIVar,
      $.giFVar,
      $.doVar,
      $.goBVar,
      $.goHVar,
      $.goIVar,
      $.goFVar
    ),

    // Assignment operator
    assignOp: ($) => seq(optional($.SPACE), "=", optional($.SPACE)),
    
    // Arithmetic operators
    arithOp: ($) => choice("+", "-", "*", "/"),
    logicOp: ($) => choice("&&", "||", "|", "&", "^"),
    shiftOp: ($) => choice("<<", ">>"),

    // Operations
    opExp: ($) => choice(
      $.copInst,
      $.ioInst,
      $.baseInst,
      $.moveInst,
      $.operateInst,
      $.controlInst
    ),

    // ********** Computation Instructions **********
    copInst: ($) => prec.left(1, choice(
      $.operateExp,
      seq("INV", $.SPACE, $.positionVar),
      seq($.intVar, $.assignOp, "NOT", $.SPACE, choice($.number, $.numVar, $.positionVarScalar)),
      seq($.floatVar, $.assignOp, choice("ATAN", "SQRT"), $.SPACE, choice($.number, $.numVar, $.positionVarScalar)),
      seq($.floatVar, $.assignOp, choice("SIN", "COS", "TAN"), $.SPACE, choice($.number, $.numVar, $.positionVarScalar)),
      seq($.floatVar, $.assignOp, "ATAN2", $.SPACE, choice($.number, $.numVar, $.positionVarScalar), $.SPACE, choice($.number, $.numVar, $.positionVarScalar)),
      seq(choice($.numVar, $.positionVarCoordScalar), $.assignOp, choice("MAX", "MIN"), $.SPACE, choice($.numVar, $.number, $.positionVarScalar), $.SPACE, choice($.numVar, $.number, $.positionVarScalar)),
      seq($.numVar, $.assignOp, choice("LOG", "POW"), $.SPACE, choice($.numVar, $.number, $.positionVarScalar), $.SPACE, choice($.numVar, $.number, $.positionVarScalar)),
      seq($.numVar, $.assignOp, "ABS", $.SPACE, choice($.numVar, $.number, $.positionVarScalar))
    )),

    operateExp: ($) => choice(
      $.numScalarOpExp,
      seq($.positionVar, $.assignOp, $.positionVar, repeat(seq($.SPACE, "+", $.SPACE, $.positionVar))),
      seq($.strVar, $.assignOp, $.strVar, repeat(seq($.SPACE, "+", $.SPACE, $.strVar)))
    ),

    numScalarOpExp: ($) => seq(
      choice($.numVar, $.positionVarCoordScalar),
      $.assignOp,
      $.copOpExp
    ),

    copOpExp: ($) => prec.left(2, seq(
      optional(seq("(", optional($.SPACE))),
      choice($.number, $.numVar, $.positionVarCoordScalar),
      repeat(seq(
        choice($.arithOp, $.logicOp, $.shiftOp),
        choice($.number, $.numVar, $.positionVarCoordScalar)
      )),
      optional(seq(optional($.SPACE), ")"))
    )),

    // ********** Base Instructions **********
    baseInst: ($) => choice(
      $.setInst,
      $.setPInst,
      seq("GETP", $.SPACE, $.positionVar),
      $.setExtAxisInst,
      $.clearInst,
      seq("STRLEN", $.SPACE, $.strVar, $.SPACE, $.iVar),
      seq("STRFIND", $.SPACE, $.strVar, $.SPACE, $.strVar, $.SPACE, $.iVar),
      seq("STRSUB", $.SPACE, $.strVar, $.SPACE, $.iVar, $.SPACE, $.iVar, $.SPACE, $.strVar),
      seq("STRCMP", $.SPACE, $.strVar, $.SPACE, $.strVar, $.SPACE, $.iVar),
      seq("GETAT", $.SPACE, $.strVar, $.SPACE, $.iVar, $.SPACE, $.strVar),
      seq("STRFINDEND", $.SPACE, $.strVar, $.SPACE, $.strVar, $.SPACE, $.iVar),
      seq("STRREVERSE", $.SPACE, $.strVar, $.SPACE, $.strVar),
      seq("STRSPLIT", $.SPACE, $.strVar, $.SPACE, $.strVar, $.SPACE, $.INT, $.SPACE, $.iVar),
      seq("SPRINTF", repeat1(seq($.SPACE, $.strVar))),
      $.setFrameInst,
      $.str2iInst,
      $.str2rInst,
      $.i2strInst,
      $.r2strInst
    ),

    setInst: ($) => choice(
      seq("SET", $.SPACE, $.positionVar, $.SPACE, $.positionVar),
      seq("SET", $.SPACE, choice($.numVar, $.positionVarScalar), $.SPACE, choice($.positionVarScalar, $.numVar, $.number)),
      seq("SET", $.SPACE, $.strVar, $.SPACE, choice($.strVar, $.STRING))
    ),

    setPInst: ($) => seq(
      "SETP",
      $.SPACE,
      $.positionVar,
      $.SPACE,
      "FRAME",
      $.SPACE,
      choice("TF", "UF"),
      $.SPACE,
      choice($.number, $.intVar)
    ),

    tnValue: ($) => seq("TN", $.SPACE, $.INT),

    setExtAxisInst: ($) => seq(
      "SETEXTAXIS",
      $.SPACE,
      $.positionVar,
      $.SPACE,
      $.ID,
      $.SPACE,
      choice($.number, $.numVar)
    ),

    clearInst: ($) => choice(
      seq("CLEAR", $.SPACE, $.iVar, $.SPACE, $.iVar),
      seq("CLEAR", $.SPACE, $.hVar, $.SPACE, $.hVar),
      seq("CLEAR", $.SPACE, $.bVar, $.SPACE, $.bVar),
      seq("CLEAR", $.SPACE, $.floatVar, $.SPACE, $.floatVar)
    ),

    setFrameInst: ($) => seq(
      "SETFRAME",
      $.SPACE,
      choice("TF", "UF"),
      $.SPACE,
      choice($.INT, $.intVar)
    ),

    str2iInst: ($) => seq("STR2I", $.SPACE, $.strVar, $.SPACE, $.iVar),
    str2rInst: ($) => seq("STR2R", $.SPACE, $.strVar, $.SPACE, $.floatVar),
    i2strInst: ($) => seq("I2STR", $.SPACE, $.iVar, $.SPACE, $.strVar),
    r2strInst: ($) => seq("R2STR", $.SPACE, $.floatVar, $.SPACE, $.strVar),

    // ********** IO Instructions **********
    ioInst: ($) => choice(
      seq("IN", $.SPACE, choice($.iVar, $.floatVar), $.assignOp, $.digitalIOVar),
      $.outExp,
      seq("ANIN", $.SPACE, $.floatVar, $.assignOp, $.anInVar),
      seq("ANOUT", $.SPACE, $.anOutVar, $.assignOp, choice($.positionVarScalar, $.numVar, $.number))
    ),

    outExp: ($) => seq(
      "OUT",
      $.SPACE,
      choice($.doVar, $.goBVar, $.goHVar, $.goIVar, $.goFVar),
      $.SPACE,
      choice($.number, $.numVar),
      optional(seq($.SPACE, $.tDIS, $.assignOp, choice($.number, $.intVar)))
    ),

    tDIS: ($) => choice("T", "DIS"),

    // ********** Control Instructions **********
    controlInst: ($) => choice(
      $.ifExp,
      $.whileExp,
      $.labelExp,
      $.gotoExp,
      $.delayExp,
      $.waitExp,
      $.callExp,
      $.paracallExp,
      seq("UALM", $.SPACE, "CODE", $.assignOp, choice($.INT, $.intVar)),
      $.switchExp,
      $.forExp,
      "PAUSE"
    ),

    ifExp: ($) => choice(
      seq("IF", $.SPACE, $.multiConExp, $.SPACE, ":", $.SPACE, choice($.gotoExp, $.callExp, $.outExp, $.delayExp, $.waitExp)),
      seq("IF", $.SPACE, $.multiConExp, $.NEWLINE, repeat1($.inst), optional(seq("ELSE", $.NEWLINE, repeat1($.inst))), "ENDIF"),
      seq("IF", $.SPACE, $.multiConExp, $.NEWLINE, repeat1($.inst), repeat(seq("ELSEIF", $.SPACE, $.multiConExp, $.NEWLINE, repeat1($.inst))), "ENDIF")
    ),

    whileExp: ($) => seq(
      "WHILE",
      $.SPACE,
      choice($.INT, $.intVar, $.multiConExp),
      $.NEWLINE,
      repeat($.whileOpExp),
      "ENDWHILE"
    ),

    whileOpExp: ($) => choice(
      $.inst,
      seq(choice("CONTINUE", "BREAK"), $.NEWLINE)
    ),

    multiConExp: ($) => prec.left(1, choice(
      seq($.conditionExp, repeat(seq($.SPACE, "AND", $.SPACE, $.conditionExp))),
      seq($.conditionExp, repeat(seq($.SPACE, "OR", $.SPACE, $.conditionExp)))
    )),

    conditionExp: ($) => seq(
      choice($.number, $.numVar, $.positionVarScalar, $.digitalIOVar),
      $.relationOp,
      choice($.number, $.numVar, $.positionVarScalar)
    ),

    relationOp: ($) => seq(
      $.SPACE,
      choice("==", "!=", ">", ">=", "<", "<="),
      $.SPACE
    ),

    labelExp: ($) => seq("LABEL", $.SPACE, $.STRING),
    gotoExp: ($) => seq("GOTO", $.SPACE, $.STRING),
    callExp: ($) => seq("CALL", $.SPACE, $.jobOrBackgroundFile),
    delayExp: ($) => seq("DELAY", $.SPACE, "T", $.assignOp, choice($.number, $.numVar)),
    waitExp: ($) => seq("WAIT", $.SPACE, $.digitalIOVar, $.assignOp, choice($.number, $.intVar), $.SPACE, "T", $.assignOp, choice($.number, $.intVar)),

    paracallExp: ($) => seq(
      choice("START_BG", "STOP_BG"),
      $.SPACE,
      $.jobOrBackgroundFile
    ),

    jobOrBackgroundFile: ($) => $.ID,
    marcoFile: ($) => $.ID,

    switchExp: ($) => seq(
      "SWITCH",
      $.SPACE,
      choice($.numVar, $.strVar),
      $.NEWLINE,
      repeat($.caseExp),
      "DEFAULT",
      $.NEWLINE,
      repeat($.switchOpExp),
      "BREAK",
      $.NEWLINE,
      "ENDSWITCH"
    ),

    caseExp: ($) => seq(
      "CASE",
      $.SPACE,
      choice($.numVar, $.strVar),
      $.NEWLINE,
      repeat($.switchOpExp),
      "BREAK",
      $.NEWLINE
    ),

    switchOpExp: ($) => choice(
      $.inst,
      seq("BREAK", $.NEWLINE)
    ),

    forExp: ($) => seq(
      "FOR",
      $.SPACE,
      $.intVar,
      $.assignOp,
      choice($.intVar, $.number),
      $.SPACE,
      $.intVar,
      $.SPACE,
      choice(">", ">=", "<", "<="),
      $.SPACE,
      choice($.intVar, $.number),
      $.SPACE,
      "STEP",
      $.SPACE,
      optional("-"),
      $.INT,
      $.NEWLINE,
      repeat($.whileOpExp),
      "ENDFOR"
    ),

    // ********** Motion Instructions **********
    moveInst: ($) => choice(
      seq("MOVJ", $.SPACE, $.positionVar, $.SPACE, $.velocityAssign, $.SPACE, $.accAssign, $.SPACE, $.crAssign, optional(seq($.SPACE, $.additionalInst))),
      seq("MOVL", $.SPACE, $.positionVar, $.SPACE, $.velocityAssign, $.SPACE, $.accAssign, $.SPACE, $.crAssign, optional(seq($.SPACE, $.additionalInst))),
      seq("MOVC", $.SPACE, $.positionVar, $.SPACE, $.velocityAssign, $.SPACE, $.accAssign, $.SPACE, $.crAssign, optional(choice(seq($.SPACE, $.additionalInst), seq($.SPACE, ":", $.SPACE, "SET", $.SPACE, "TYPE", $.assignOp, $.INT)))),
      seq("MOVS", $.SPACE, $.positionVar, $.SPACE, $.velocityAssign, $.SPACE, $.accAssign, $.SPACE, $.crAssign),
      seq("JUMP", $.SPACE, $.positionVar, $.SPACE, $.velocityAssign, $.SPACE, $.accAssign, $.SPACE, $.crAssign, $.SPACE, $.lhAssign, $.SPACE, $.mhAssign, $.SPACE, $.rhAssign),
      seq("JUMPL", $.SPACE, $.positionVar, $.SPACE, $.velocityAssign, $.SPACE, $.accAssign, $.SPACE, $.crAssign, $.SPACE, $.lhAssign, $.SPACE, $.mhAssign, $.SPACE, $.rhAssign),
      seq("ARMCHANGE", $.SPACE, choice("LEFT", "RIGHT", "AUTO"), $.SPACE, $.velocityAssign)
    ),

    velocityAssign: ($) => seq("V", $.assignOp, choice($.number, $.numVar)),
    accAssign: ($) => seq("ACC", $.assignOp, $.number),
    cntAssign: ($) => seq("CNT", $.assignOp, $.number),
    crAssign: ($) => choice($.cntAssign, "FINE"),

    additionalInst: ($) => seq(
      ":",
      $.SPACE,
      choice(
        $.skipLaExp,
        $.outExp,
        seq("TOOL_OFFSET", optional(seq($.SPACE, $.positionVar))),
        seq("USER_OFFSET", optional(seq($.SPACE, $.positionVar)))
      )
    ),

    skipLaExp: ($) => seq("SKIP", $.SPACE, $.STRING),

    lhAssign: ($) => seq("LH", $.assignOp, $.number),
    mhAssign: ($) => seq("MH", $.assignOp, $.number),
    rhAssign: ($) => seq("RH", $.assignOp, $.number),

    // ********** Operation Instructions **********
    operateInst: ($) => choice(
      $.irqOnExp,
      $.irqOffExp,
      seq("TIMER START", $.SPACE, "INDEX", $.assignOp, $.INT),
      seq("TIMER STOP", $.SPACE, "INDEX", $.assignOp, $.INT, $.SPACE, choice($.iVar, $.floatVar)),
      seq("SKIP", $.SPACE, "CONDITION", $.SPACE, $.digitalIOVar, $.SPACE, "==", $.SPACE, $.number),
      seq("USER_OFFSET_CONDITION", $.SPACE, $.positionVar, $.SPACE, $.ufValue),
      seq("TOOL_OFFSET_CONDITION", $.SPACE, $.positionVar, $.SPACE, $.tfValue),
      seq("RECORDMOTIONSTART", $.SPACE, $.tAssign),
      seq("RECORDMOTIONSTOP", $.SPACE, $.fAssign),
      seq("TCPCREATE", $.SPACE, $.STRING, $.SPACE, "SERVERIP", $.assignOp, $.ID, $.SPACE, "PORT", $.assignOp, choice($.INT, $.intVar)),
      seq("TCPSEND", $.SPACE, $.STRING, $.SPACE, choice($.strVar, $.number)),
      seq("TCPRECV", $.SPACE, $.STRING, $.SPACE, choice($.number, $.strVar), $.SPACE, choice($.number, $.intVar), $.SPACE, "TIMEOUT", $.assignOp, choice($.number, $.intVar), $.SPACE, $.iVar),
      seq("TCPCLOSE", $.SPACE, $.STRING)
    ),

    irqOnExp: ($) => seq(
      "IRQON",
      $.SPACE,
      $.levAssign,
      $.SPACE,
      choice($.diVar, $.iVar),
      $.SPACE,
      "==",
      $.SPACE,
      $.number,
      $.SPACE,
      $.jobOrBackgroundFile
    ),

    irqOffExp: ($) => seq("IRQOFF", $.SPACE, $.levAssign),

    levAssign: ($) => seq("LEV", $.assignOp, $.INT),
    ufValue: ($) => seq("UF", "[", $.varIndex, "]"),
    tfValue: ($) => seq("TF", "[", $.varIndex, "]"),
    fAssign: ($) => seq("F", $.assignOp, choice($.number, $.intVar)),
    tAssign: ($) => seq("T", $.assignOp, choice($.number, $.intVar)),
  },
});
