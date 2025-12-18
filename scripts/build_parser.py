from tree_sitter import Language, Parser
import tree_sitter_siasun

# For newer tree-sitter versions, use the language directly
# If you need to build from source, install tree-sitter-cli and use:
# tree-sitter build --output siasun_robot.so tree-sitter-siasun
# 
# Or if using the older API available in some versions:
import tree_sitter
if hasattr(tree_sitter, 'Language') and hasattr(tree_sitter.Language, 'build_library'):
    tree_sitter.Language.build_library(
        'siasun_robot.so',
        ['tree-sitter-siasun']
    )
else:
    # Use the language binding directly
    SIASUN_LANGUAGE = tree_sitter_siasun.language()

print("Build complete: siasun_robot.so generated.")