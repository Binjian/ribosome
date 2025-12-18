/**
 * @file grammar.js
 * @author Siasun Robot Language Support
 * @license MIT
 */

/// <reference types="tree-sitter-cli/dsl" />

module.exports = grammar({
  name: "siasun_robot",

  extras: ($) => [
    /\s/, // Whitespace
    $.comment,
  ],

  conflicts: ($) => [
    // Conflict resolution between variable types and identifiers if necessary
    [$._value, $.variable],
  ],

  rules: {
    source_file: ($) => repeat($._statement),

    _statement: ($) =>
      choice(
        $.program_header,
        $.program_footer,
        $.label_definition,
        $.motion_statement,
        $.logic_statement,
        $.io_statement,
        $.control_statement,
        $.macro_statement,
        $.socket_statement, // Added based on [1]
        $.empty_line
      ),

    empty_line: ($) => /[\r\n]+/,

    comment: ($) =>
      token(
        choice(
          seq("//", /.*/),
          seq(";", /.*/) // Source [2] mentions COMMENT format
        )
      ),

    // --- Program Structure [3] ---
    program_header: ($) => "NOP",
    program_footer: ($) => "END",

    // --- Identifiers & Variables [4] ---
    identifier: ($) => /[a-zA-Z_][a-zA-Z0-9_]*/,

    // Line Numbers (Optional, usually editor generated but can appear in text)
    line_number: ($) => /\d+/,

    // Variables [4]
    variable: ($) =>
      choice(
        $.var_i, // Integer: I1-I520
        $.var_r, // Real: R1-R520
        $.var_b, // Byte: B1-B100
        $.var_s, // String: S1 or Str1
        $.var_ls, // Local String: LS#1
        $.var_p, // Local Position: P1
        $.var_pr, // Global Position: PR1
        $.var_si, // System Integer
        $.var_sr, // System Real
        $.var_io, // IO variables: IN#1, OUT#1
        $.var_gio // Group IO: IG#1, OG#1
      ),

    var_i: ($) => /I\d+/,
    var_r: ($) => /R\d+/,
    var_b: ($) => /B\d+/,
    var_s: ($) => /(Str|S#)\d+/,
    var_ls: ($) => /LS#\d+/,
    // Position variables can have sub-fields (e.g., PR1.1) [5]
    var_p: ($) => seq(/P\d+/, optional(seq(".", /\d+/))),
    var_pr: ($) => seq(/PR\d+/, optional(seq(".", /\d+/))),
    var_si: ($) => /SI\d+/,
    var_sr: ($) => /SR\d+/,

    // IO Variables [6]
    var_io: ($) => choice(/IN#\d+/, /OT#\d+/, /OUT#\d+/, /AIN#\d+/, /AOUT#\d+/),
    var_gio: ($) =>
      choice(/IG#\d+/, /OG#\d+/, /IGH#\d+/, /OGH#\d+/, /IGFA#\d+/, /OGFA#\d+/),

    // Literals
    number: ($) =>
      choice(
        /\d+(\.\d+)?/, // Floating point or Integer
        /-\d+(\.\d+)?/
      ),

    string_literal: ($) => /"[^"]*"/,

    // --- Motion Statements [7-16] ---
    motion_statement: ($) =>
      seq(
        choice("MOVJ", "MOVL", "MOVC", "MOVSPI", "MOVCIRA"),
        field("target", choice($.var_p, $.var_pr)),
        repeat($.motion_param),
        optional("ET"), // External Tool [9]
        optional($._statement_terminator)
      ),

    motion_param: ($) =>
      choice(
        seq("VJ=", $._value), // Joint Speed %
        seq("VL=", $._value), // Linear Speed mm/s
        seq("VC=", $._value), // Circular Speed
        seq("ACC=", $._value), // Acceleration
        seq("CNT=", $._value), // Continuous trajectory (0-100)
        seq("R=", $._value), // Radius (for CIRA)
        seq("D=", $._value), // Distance (for SPI)
        seq("SD=", $._value), // Stop Distance (for SPI)
        seq("RTYPE=", $._value),
        seq("OFFSET=", $._value),
        seq("OVER=", $._value),
        seq("UF=", $._value),
        seq("INTERLOCK=", $._value) // [17]
      ),

    // --- Logic & Calculation Statements [4, 5, 18-27] ---
    logic_statement: ($) =>
      choice($.assignment, $.math_operation, $.unary_operation),

    assignment: ($) =>
      seq(
        "SET",
        field("destination", $.variable),
        optional("="), // Manual shows SET I1=100 and SET I1 I2
        field("source", $._value),
        // Specific handling for "N" batch assignment [22]
        optional(seq("N=", $._value, optional($.variable)))
      ),

    math_operation: ($) =>
      seq(
        choice("ADD", "SUB", "MUL", "DIV", "SQRT", "SIN", "COS", "ATAN"),
        field("operand1", $.variable),
        field("operand2", $._value),
        optional(field("destination", $.variable)) // Some ops store in operand1, some have dest [5]
      ),

    unary_operation: ($) =>
      seq(
        choice("INC", "DEC", "INT"), // INT is cast to int
        field("operand", $.variable),
        optional(field("source", $.variable)) // For INT I1 R1
      ),

    // --- I/O Statements [28-34] ---
    io_statement: ($) =>
      choice(
        $.out_statement,
        $.pulse_statement,
        $.wait_statement,
        $.analog_io_statement
      ),

    out_statement: ($) =>
      seq(
        choice("OUT", "OUT_P", "OUT_T"),
        field("port", choice($.var_io, $.var_gio, $.variable)),
        "=",
        field("value", choice("ON", "OFF", $._value)),
        optional(seq(choice("D=", "T="), $._value)) // For OUT_P and OUT_T
      ),

    pulse_statement: ($) =>
      seq(
        "PULSE",
        field("port", $.var_io),
        "=",
        choice("ON", "OFF"),
        "T=",
        $._value // Duration
      ),

    wait_statement: ($) =>
      seq(
        "WAIT",
        $.condition_expression,
        optional(seq("T=", $._value)) // Timeout (-1 for infinite)
      ),

    analog_io_statement: ($) =>
      seq(
        choice("AIN", "AOUT"),
        choice(
          seq($.var_r, /AI\d+/), // AIN R1 AI1
          seq(/AO\d+/, "=", $._value) // AOUT AO1 = 5.0
        )
      ),

    // --- Control Flow Statements [2, 32, 35-45] ---
    control_statement: ($) =>
      choice(
        $.goto_statement,
        $.call_statement,
        $.return_statement,
        $.if_statement,
        $.select_statement,
        $.loop_statement,
        $.interrupt_statement, // IRQ
        $.coord_statement // SET TF/UF
      ),

    label_definition: ($) =>
      seq(
        choice("LABEL", "L"),
        field("label_name", choice($.number, $.identifier))
      ),

    goto_statement: ($) =>
      seq(
        "GOTO",
        choice("L", "LABEL"),
        field("target", choice($.number, $.identifier))
      ),

    call_statement: ($) =>
      seq(
        "CALL",
        // CALL can have a condition prefix [35] e.g. CALL IN#1=1 SUB1
        optional($.condition_expression),
        field("program", choice($.identifier, $.var_s)),
        optional(seq("JOB", $._value)) // For cases like A01_CMN JobOrder
      ),

    return_statement: ($) => choice("RET", "RESUME"), // RESUME for interrupts

    // IF Structures [36-40]
    if_statement: ($) =>
      choice(
        // Simple line IF: IF I1=1 L10
        seq("IF", $.condition_expression, $.goto_target),
        // Block IF: IF THEN ... ENDIF
        seq(
          "IF",
          "THEN",
          $.condition_expression,
          repeat($._statement),
          optional($.else_block),
          "ENDIF"
        )
      ),

    else_block: ($) =>
      seq(
        choice("ELSE", seq("ELSE", "IF", $.condition_expression)),
        repeat($._statement)
      ),

    goto_target: ($) =>
      seq(choice("L", "LABEL"), choice($.number, $.identifier)),

    // SELECT Structure [38]
    select_statement: ($) =>
      seq(
        "SELECT",
        $.condition_expression,
        choice(seq("CALL", $.identifier), seq("GOTO", $.goto_target))
      ),

    // WHILE / Loops [43]
    loop_statement: ($) =>
      seq(
        "WHILE",
        $._value,
        $.comparison_operator,
        $._value,
        $.goto_target // Documentation shows WHILE jumping to a Label
      ),

    // Interrupts [46]
    interrupt_statement: ($) =>
      seq(
        choice("IRQON", "IRQOFF"),
        optional(seq("LEV=", $._value)), // Priority
        optional($.condition_expression),
        optional($.identifier) // Interrupt program name
      ),

    // Coordinate System [47]
    coord_statement: ($) => seq("SET", choice("TF", "UF"), "#", $._value),

    // Macro Call [39]
    macro_statement: ($) => seq("MACRO", $._value),

    // Socket/String [1]
    socket_statement: ($) =>
      seq(
        choice("TCPCREATE", "TCPSEND", "TCPRECV", "TCPCLOSE"),
        repeat(seq($.identifier, "=", $._value)) // Key=Value style arguments
      ),

    // --- Shared Expressions ---

    // Condition: I1 = 100, IN#1 = ON, R1 > 5.0
    condition_expression: ($) =>
      seq(
        choice($.variable, $.var_io, $.var_gio),
        $.comparison_operator,
        $._value
      ),

    comparison_operator: ($) => choice("=", "==", ">", "<", "<>", ">=", "<="),

    _value: ($) =>
      choice(
        $.number,
        $.string_literal,
        $.variable,
        $.identifier, // For ON/OFF/TRUE/FALSE or constants
        "ON",
        "OFF"
      ),

    _statement_terminator: ($) => /[\r\n]+/,
  },
});
