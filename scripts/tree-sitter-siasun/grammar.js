module.exports = grammar({
  name: "test_simple",
  
  extras: ($) => [$.SPACE],
  
  rules: {
    prog: ($) => seq(
      "START",
      $.NEWLINE,
      repeat($.stmt),
      "END"
    ),
    
    stmt: ($) => seq($.assign, $.NEWLINE),
    
    assign: ($) => seq($.var, "=", $.var),
    
    var: ($) => seq("X", "[", $.INT, "]"),
    
    INT: ($) => /[0-9]+/,
    NEWLINE: ($) => /\r?\n/,
    SPACE: ($) => / +/,
  }
});
