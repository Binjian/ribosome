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

  word: ($) => $.identifier,

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
        $.socket_statement
      ),

    comment: ($) =>
      token(
        choice(
          seq("//", /.*/),
          seq(";", /.*/)
        )
      ),

    // --- Program Structure ---
    program_header: ($) => "NOP",
    program_footer: ($) => "END",

    // --- Identifiers & Variables ---
    identifier: ($) => /[a-zA-Z_][a-zA-Z0-9_]*/,

    // Variables
    variable: ($) =>
      choice(
        $.var_i,   // Integer: I1-I520
        $.var_r,   // Real: R1-R520
        $.var_b,   // Byte: B1-B100
        $.var_s,   // String: S1 or Str1
        $.var_ls,  // Local String: LS#1
        $.var_p,   // Local Position: P1
        $.var_pr,  // Global Position: PR1
        $.var_si,  // System Integer
        $.var_sr,  // System Real
        $.var_io,  // IO variables: IN#1, OUT#1
        $.var_gio  // Group IO: IG#1, OG#1
      ),

    var_i: ($) => /I\d+/,
    var_r: ($) => /R\d+/,
    var_b: ($) => /B\d+/,
    var_s: ($) => /(Str|S#)\d+/,
    var_ls: ($) => /LS#\d+/,
    var_p: ($) => /P\d+(\.\d+)?/,
    var_pr: ($) => /PR\d+(\.\d+)?/,
    var_si: ($) => /SI\d+/,
    var_sr: ($) => /SR\d+/,
    var_io: ($) => /(IN|OT|OUT|AIN|AOUT)#\d+/,
    var_gio: ($) => /(IG|OG|IGH|OGH|IGFA|OGFA)#\d+/,

    // Literals
    number: ($) => /-?\d+(\.\d+)?/,
    string_literal: ($) => /"[^"]*"/,

    // Boolean values as keywords
    bool_value: ($) => choice("ON", "OFF", "TRUE", "FALSE"),

    // --- Motion Statements ---
    motion_statement: ($) =>
      choice(
        // MOVC with via point and end point
        seq(
          field("command", "MOVC"),
          field("via_point", choice($.var_p, $.var_pr)),
          field("target", choice($.var_p, $.var_pr)),
          repeat($.motion_param),
          optional("ET")
        ),
        // Standard motion commands
        seq(
          field("command", choice("MOVJ", "MOVL", "MOVSPI", "MOVCIRA")),
          field("target", choice($.var_p, $.var_pr)),
          repeat($.motion_param),
          optional("ET")
        )
      ),

    motion_param: ($) =>
      choice(
        // Regular single-value parameters
        seq(
          field("param_name", choice(
            "VJ=", "VL=", "VC=", "ACC=", "CNT=", "R=", "D=", "SD=",
            "RTYPE=", "OVER=", "UF=", "INTERLOCK="
          )),
          field("param_value", $._value)
        ),
        // OFFSET with comma-separated values (X,Y,Z,Rx,Ry,Rz)
        seq(
          "OFFSET=",
          $.offset_values
        )
      ),

    offset_values: ($) =>
      seq(
        $.offset_component,
        repeat(seq(",", $.offset_component))
      ),

    offset_component: ($) =>
      choice($.number, $.var_r, $.var_i, $.identifier),

    // --- Logic & Calculation Statements ---
    logic_statement: ($) =>
      choice($.assignment, $.math_operation, $.unary_operation),

    assignment: ($) =>
      seq(
        "SET",
        field("destination", $.variable),
        optional("="),
        field("source", $._value),
        optional(seq("N=", $._value, optional($.variable)))
      ),

    math_operation: ($) =>
      seq(
        field("op", choice("ADD", "SUB", "MUL", "DIV", "SQRT", "SIN", "COS", "ATAN")),
        field("operand1", $.variable),
        field("operand2", $._value),
        optional(field("destination", $.variable))
      ),

    unary_operation: ($) =>
      seq(
        field("op", choice("INC", "DEC", "INT")),
        field("operand", $.variable),
        optional(field("source", $.variable))
      ),

    // --- I/O Statements ---
    io_statement: ($) =>
      choice(
        $.out_statement,
        $.pulse_statement,
        $.wait_statement,
        $.analog_io_statement
      ),

    out_statement: ($) =>
      seq(
        field("command", choice("OUT", "OUT_P", "OUT_T")),
        field("port", $.variable),
        "=",
        field("value", $._value),
        optional(seq(choice("D=", "T="), $._value))
      ),

    pulse_statement: ($) =>
      seq(
        "PULSE",
        field("port", $.var_io),
        "=",
        $.bool_value,
        "T=",
        $._value
      ),

    wait_statement: ($) =>
      seq(
        "WAIT",
        $.condition_expression,
        optional(seq("T=", $._value))
      ),

    analog_io_statement: ($) =>
      seq(
        choice("AIN", "AOUT"),
        choice(
          seq($.var_r, /AI\d+/),
          seq(/AO\d+/, "=", $._value)
        )
      ),

    // --- Control Flow Statements ---
    control_statement: ($) =>
      choice(
        $.goto_statement,
        $.call_statement,
        $.return_statement,
        $.if_statement,
        $.select_statement,
        $.loop_statement,
        $.interrupt_statement,
        $.coord_statement
      ),

    label_definition: ($) =>
      choice(
        // Standard label: LABEL name or L name
        prec(2, seq(
          field("keyword", choice("LABEL", "L")),
          field("label_name", $.label_name)
        )),
        // Standalone label: L10 (L followed immediately by number)
        prec(3, /L\d+/)
      ),

    label_name: ($) => choice($.number, $.identifier),

    goto_statement: ($) =>
      seq("GOTO", $.goto_target),

    goto_target: ($) =>
      choice(
        // Standalone label reference: L10
        prec(2, /L\d+/),
        // Keyword-based reference: L name or LABEL name
        prec(1, seq(
          choice("L", "LABEL"),
          field("target", $.label_name)
        ))
      ),

    call_statement: ($) =>
      seq(
        "CALL",
        optional($.condition_expression),
        field("program", choice($.identifier, $.var_s)),
        optional(seq("JOB", $._value))
      ),

    return_statement: ($) => choice("RET", "RESUME"),

    // IF Structures
    if_statement: ($) =>
      choice(
        // Simple line IF: IF I1=1 L10
        prec(2, seq("IF", $.condition_expression, $.goto_target)),
        // Block IF: IF THEN ... ENDIF
        prec(1, seq(
          "IF", "THEN",
          $.condition_expression,
          repeat($._statement),
          optional($.else_clause),
          "ENDIF"
        ))
      ),

    else_clause: ($) =>
      choice(
        seq("ELSE", repeat($._statement)),
        seq("ELSEIF", $.condition_expression, repeat($._statement), optional($.else_clause))
      ),

    // SELECT Structure
    select_statement: ($) =>
      seq(
        "SELECT",
        $.condition_expression,
        choice(
          seq("CALL", $.identifier),
          seq("GOTO", $.goto_target)
        )
      ),

    // WHILE Loop
    loop_statement: ($) =>
      seq(
        "WHILE",
        $._value,
        $.comparison_operator,
        $._value,
        $.goto_target
      ),

    // Interrupts
    interrupt_statement: ($) =>
      seq(
        choice("IRQON", "IRQOFF"),
        optional(seq("LEV=", $._value)),
        optional($.condition_expression),
        optional($.identifier)
      ),

    // Coordinate System (handled by macro_statement now)
    coord_statement: ($) =>
      seq("SET", choice("TF", "UF"), "#", $._value),

    // Tool/User Frame commands
    macro_statement: ($) => choice(
      seq("MACRO", $._value),
      seq(choice("TF", "UF"), "#", $.number)
    ),

    // Socket/String
    socket_statement: ($) =>
      seq(
        choice("TCPCREATE", "TCPSEND", "TCPRECV", "TCPCLOSE"),
        repeat(seq($.identifier, "=", $._value))
      ),

    // --- Shared Expressions ---
    condition_expression: ($) =>
      prec(2, seq(
        field("left", $.variable),
        field("operator", $.comparison_operator),
        field("right", $._value)
      )),

    comparison_operator: ($) => choice("=", "==", ">", "<", "<>", ">=", "<="),

    _value: ($) =>
      choice(
        $.number,
        $.string_literal,
        $.variable,
        $.bool_value,
        $.identifier
      ),
  },
});
