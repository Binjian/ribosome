"""
Convert Tree-sitter grammar.json to GBNF (GGML BNF) format

GBNF is used by llama.cpp for constrained text generation.
This script converts a tree-sitter grammar to GBNF format.

Usage:
    python grammar_to_gbnf.py grammar.json output.gbnf
"""

import json
import sys
import re
from pathlib import Path
from typing import Dict, List, Any


class GBNFConverter:
    """Convert tree-sitter grammar.json to GBNF format"""
    
    def __init__(self):
        self.rules: Dict[str, str] = {}
        self.processed: set = set()
        
    def convert_grammar(self, grammar_path: str, output_path: str) -> None:
        """Convert grammar.json to GBNF"""
        with open(grammar_path, 'r') as f:
            grammar = json.load(f)
        
        # Extract rules
        rules = grammar.get('rules', {})
        
        # Convert each rule
        for rule_name, rule_def in rules.items():
            self._convert_rule(rule_name, rule_def)
        
        # Write GBNF output
        with open(output_path, 'w') as f:
            # Write root rule first
            if 'source_file' in self.rules:
                f.write(f"root ::= {self.rules['source_file']}\n\n")
            
            # Write other rules
            for rule_name in sorted(self.rules.keys()):
                if rule_name != 'source_file':
                    f.write(f"{rule_name} ::= {self.rules[rule_name]}\n")
        
        print(f"Converted grammar to GBNF: {output_path}")
        print(f"Total rules: {len(self.rules)}")
    
    def _convert_rule(self, name: str, definition: Any) -> str:
        """Convert a single rule definition"""
        if name in self.processed:
            return name
        
        self.processed.add(name)
        rule_body = self._convert_node(definition)
        self.rules[name] = rule_body
        return name
    
    def _convert_node(self, node: Any) -> str:
        """Convert a grammar node to GBNF expression"""
        if isinstance(node, str):
            # String literal
            return self._escape_string(node)
        
        if not isinstance(node, dict):
            return '""'
        
        node_type = node.get('type', '')
        
        if node_type == 'STRING':
            # Terminal string
            value = node.get('value', '')
            return self._escape_string(value)
        
        elif node_type == 'PATTERN':
            # Regex pattern - convert to character class or sequence
            pattern = node.get('value', '')
            return self._convert_pattern(pattern)
        
        elif node_type == 'SYMBOL':
            # Reference to another rule
            return node.get('name', '')
        
        elif node_type == 'CHOICE':
            # Alternatives: a | b | c
            members = node.get('members', [])
            choices = [self._convert_node(m) for m in members]
            return '(' + ' | '.join(choices) + ')'
        
        elif node_type == 'SEQ':
            # Sequence: a b c
            members = node.get('members', [])
            parts = [self._convert_node(m) for m in members]
            # Filter empty strings
            parts = [p for p in parts if p and p != '""']
            if not parts:
                return '""'
            return ' '.join(parts)
        
        elif node_type == 'REPEAT':
            # Zero or more: a*
            content = node.get('content', {})
            inner = self._convert_node(content)
            return f"({inner})*"
        
        elif node_type == 'REPEAT1':
            # One or more: a+
            content = node.get('content', {})
            inner = self._convert_node(content)
            return f"({inner})+"
        
        elif node_type == 'FIELD':
            # Field wrapper - just return the content
            content = node.get('content', {})
            return self._convert_node(content)
        
        elif node_type == 'PREC' or node_type == 'PREC_LEFT' or node_type == 'PREC_RIGHT':
            # Precedence - ignore in GBNF, just return content
            content = node.get('content', {})
            return self._convert_node(content)
        
        elif node_type == 'TOKEN':
            # Token - just return content
            content = node.get('content', {})
            return self._convert_node(content)
        
        elif node_type == 'IMMEDIATE_TOKEN' or node_type == 'ALIAS':
            # Immediate token or alias - return content
            content = node.get('content', {})
            return self._convert_node(content)
        
        elif node_type == 'BLANK':
            # Empty/blank
            return '""'
        
        else:
            # Unknown type - try to get content
            content = node.get('content', {})
            if content:
                return self._convert_node(content)
            return '""'
    
    def _escape_string(self, s: str) -> str:
        """Escape string for GBNF"""
        if not s:
            return '""'
        
        # Escape special characters
        s = s.replace('\\', '\\\\')
        s = s.replace('"', '\\"')
        s = s.replace('\n', '\\n')
        s = s.replace('\r', '\\r')
        s = s.replace('\t', '\\t')
        
        return f'"{s}"'
    
    def _convert_pattern(self, pattern: str) -> str:
        """Convert regex pattern to GBNF character class"""
        # Simple patterns
        if pattern == r'\s':
            return r'[ \t\n\r]'
        elif pattern == r'\d':
            return '[0-9]'
        elif pattern == r'\w':
            return '[a-zA-Z0-9_]'
        elif pattern == r'[a-zA-Z_][a-zA-Z0-9_]*':
            # Identifier pattern
            return '[a-zA-Z_] [a-zA-Z0-9_]*'
        elif pattern == r'-?\d+(\.\d+)?':
            # Number pattern
            return '("-"?) [0-9]+ ("." [0-9]+)?'
        elif pattern.startswith('[') and pattern.endswith(']'):
            # Character class - use as is
            return pattern
        elif pattern.startswith('/') and pattern.endswith('/'):
            # Regex delimiters - remove them
            pattern = pattern[1:-1]
            return self._convert_pattern(pattern)
        else:
            # Try to convert common patterns
            # For complex patterns, approximate with character class
            if '.*' in pattern:
                return '[\\x00-\\xFF]*'  # Any characters
            elif '.+' in pattern:
                return '[\\x00-\\xFF]+'  # Any characters (at least one)
            else:
                # Quote as literal string
                return self._escape_string(pattern)
    
    def _simplify_expression(self, expr: str) -> str:
        """Simplify GBNF expression"""
        # Remove redundant parentheses
        # Remove empty alternatives
        expr = re.sub(r'\(\s*\)', '', expr)
        expr = re.sub(r'\|\s*\|', '|', expr)
        expr = re.sub(r'\s+', ' ', expr)
        return expr.strip()


def main():
    """Main entry point"""
    if len(sys.argv) < 2:
        grammar_file = Path(__file__).parent / "tree-sitter-siasun" / "src" / "grammar.json"
        output_file = Path(__file__).parent / "tree-sitter-siasun" / "siasun_robot.gbnf"
    else:
        grammar_file = Path(sys.argv[1])
        output_file = Path(sys.argv[2]) if len(sys.argv) > 2 else grammar_file.with_suffix('.gbnf')
    
    if not grammar_file.exists():
        print(f"Error: Grammar file not found: {grammar_file}")
        sys.exit(1)
    
    converter = GBNFConverter()
    converter.convert_grammar(str(grammar_file), str(output_file))
    
    print(f"\nGBNF grammar written to: {output_file}")
    print("\nUsage with llama.cpp:")
    print(f"  ./main -m model.gguf --grammar-file {output_file.name}")


if __name__ == "__main__":
    main()
