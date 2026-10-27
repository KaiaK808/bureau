"""Placement fixtures shared by validator and real provider/app finish tests."""
from textwrap import indent

GOVERNANCE = ('## Governance and security\nDecision: not required\n'
              'Rationale: This documentation change handles no credentials or personal data.\n')
# Every first fence-like line is a cutoff, regardless of closer syntax.
# Keep earlier cases so all formerly accepted post-fence placements are tested.
CONTAINER_FENCES = {
    'list_item': ('- ```md\n', '  ', '  ```\n'),
    'asterisk_list': ('* ```md\n', '  ', '  ```\n'),
    'plus_list_tildes': ('+ ~~~md\n', '  ', '  ~~~\n'),
    'ordered_dot': ('1. ```md\n', '   ', '   ```\n'),
    'ordered_paren': ('1) ~~~md\n', '   ', '   ~~~\n'),
    'blockquote': ('> ```md\n', '> ', '> ```\n'),
    'nested_list': ('- outer\n  1. ```md\n', '     ', '     ```\n'),
    'nested_quote_list': ('> 1) - > ````md\n', '>    ', '> + 2. ````` \t\n'),
    'tab_indentation': ('\t> + ~~~~md\n', '\t', '\t> ~~~~\n'),
    'compact_dash': ('-```md\n', '  ', '-```\n'),
    'compact_asterisk': ('*~~~md\n', '  ', '*~~~\n'),
    'compact_plus': ('+```md\n', '  ', '+```\n'),
    'compact_ordered_dot': ('1.```md\n', '   ', '1.```\n'),
    'compact_ordered_paren': ('1)~~~md\n', '   ', '1)~~~\n'),
    'ordered_wide_dot': ('123. ```md\n', '     ', '     ```\n'),
    'ordered_wide_paren': ('12345) ~~~md\n', '       ', '       ~~~\n'),
    'padded_list': ('-    ```md\n', '     ', '     ```\n'),
    'nested_wide_list': ('- outer\n  123. ```md\n', '       ', '       ```\n'),
    'tab_padded_list': ('-\t~~~md\n', '\t', '\t~~~\n'),
    'tab_padded_ordered': ('1.\t```md\n', '\t', '\t```\n'),
    'three_space_closer': ('```md\n', '', '   ```\n'),
    'relative_three_column_closer': ('    ```md\n', '    ', '       ```\n'),
    'tab_next_stop_from_one_space': (' ```md\n', ' ', ' \t```\n'),
    'tab_next_stop_after_marker': ('123.\t```md\n', '        ', '        ```\n'),
    'relative_tab_opener': ('\t~~~md\n', '\t', '       ~~~\n'),
}
FENCED_GOVERNANCE = {}
AFTER_FENCE_GOVERNANCE = {}
for name, (opener, prefix, closer) in CONTAINER_FENCES.items():
    FENCED_GOVERNANCE[name] = opener + indent(GOVERNANCE, prefix) + closer
    # Column-zero content after a fence must fail independently of heading checks.
    FENCED_GOVERNANCE[name+'_column_zero'] = opener + GOVERNANCE + closer
    AFTER_FENCE_GOVERNANCE[name] = opener + prefix + 'example\n' + closer + GOVERNANCE
FENCED_GOVERNANCE.update({
    'unclosed_list': '- ```md\n' + indent(GOVERNANCE, '  '),
    'unclosed_blockquote': '> ~~~md\n' + GOVERNANCE,
    'unclosed_nested_list': '- outer\n  1) ```md\n' + GOVERNANCE,
    'short_container_closer': '- ````md\n  ```\n' + GOVERNANCE + '  ````\n',
    'trailing_container_closer': '> ~~~md\n> ~~~ trailing text\n' + GOVERNANCE + '> ~~~\n',
    'mismatched_container_closer': '- ```md\n  ~~~\n' + GOVERNANCE + '  ```\n',
    'fenced_decision': '## Governance and security\n- ```md\n  Decision: not required\n  ```\nRationale: All contents remain static.\n',
    'fenced_rationale': '## Governance and security\nDecision: not required\n- ```md\n  Rationale: All contents remain static.\n  ```\n',
    'excessive_space_closer': '```md\n    ```\n' + GOVERNANCE + '```\n',
    'excessive_tilde_closer': '~~~md\n    ~~~\n' + GOVERNANCE + '~~~\n',
    'excessive_tab_closer': '```md\n\t```\n' + GOVERNANCE + '```\n',
    'relative_four_column_closer': '    ```md\n        ```\n' + GOVERNANCE + '    ```\n',
    'relative_tab_four_column_closer': '\t~~~md\n\t\t~~~\n' + GOVERNANCE + '\t~~~\n',
    'tab_next_stop_from_zero': '```md\n \t```\n' + GOVERNANCE + '```\n',
    'list_excessive_closer': '- ```md\n      ```\n' + GOVERNANCE + '  ```\n',
    'quote_excessive_closer': '> ```md\n>     ```\n' + GOVERNANCE + '> ```\n',
    'ordered_excessive_closer': '123. ```md\n         ```\n' + GOVERNANCE + '     ```\n',
})

# Round 5: shorter/trailing/mismatched closers cannot expose governance.
for marker in ('`', '~'):
    for name, opener, closer in (
        ('four_character', marker*4, marker*3),
        ('trailing_text', marker*3, marker*3+' trailing text'),
        ('mismatched', marker*3, ('~' if marker == '`' else '`')*3),
    ):
        FENCED_GOVERNANCE[name+'_'+str(ord(marker))] = opener+'md\n'+closer+'\n'+GOVERNANCE+opener+'\n'
    FENCED_GOVERNANCE['unclosed_root_'+str(ord(marker))] = marker*3+'md\n'+GOVERNANCE
    # Round 7: container-prefixed false closers of root fences.
    for prefix in ('- ', '* ', '+ ', '1. ', '1) ', '> ', '-', '*', '+', '1.', '1)', '>', '> 1) - > '):
        FENCED_GOVERNANCE['false_closer_'+str(ord(marker))+'_'+repr(prefix)] = marker*3+'md\n'+prefix+marker*3+'\n'+GOVERNANCE+marker*3+'\n'
    for spaces in range(1, 4):
        FENCED_GOVERNANCE['four_space_closer_'+str(ord(marker))+'_'+str(spaces)] = ' '*spaces+marker*3+'md\n    '+marker*3+'\n'+GOVERNANCE+marker*3+'\n'
FENCED_GOVERNANCE['backtick_in_info'] = '```invalid`info\n'+GOVERNANCE

# Earlier root-fence acceptance cases are retained with reversed expectations.
AFTER_FENCE_GOVERNANCE.update({
    'root_backtick': '```md\nexample\n```\n'+GOVERNANCE,
    'root_long_backtick': '````md\n```\nexample\n`````\n'+GOVERNANCE,
    'root_tilde': '~~~md\nexample\n~~~\n'+GOVERNANCE,
    'root_long_tilde': '~~~~md\n~~~\nexample\n~~~~~\n'+GOVERNANCE,
    'three_space_root_backtick': '   ```md\nexample\n   ``` \t\n'+GOVERNANCE,
    'three_space_root_tilde': '   ~~~md\nexample\n   ~~~~\t\n'+GOVERNANCE,
    'one_space_root_closer': '```md\nexample\n ```\n'+GOVERNANCE,
    'two_space_tilde_closer': '~~~md\nexample\n  ~~~\n'+GOVERNANCE,
})
# A properly closed block before governance is deliberately rejected now.
FENCED_GOVERNANCE.update({'after_closed_'+name: body for name, body in AFTER_FENCE_GOVERNANCE.items()})
GOVERNANCE_FIRST = {
    name: GOVERNANCE+opener+prefix+'example\n'+closer
    for name, (opener, prefix, closer) in CONTAINER_FENCES.items()
}

COMMENT_HIDDEN_GOVERNANCE = {
    'whole_section': '<!--\n'+GOVERNANCE+'-->\n',
    'unclosed_section': '<!--\n'+GOVERNANCE,
    'heading': '<!-- ## Governance and security -->\n'+GOVERNANCE.split('\n',1)[1],
    'decision': '## Governance and security\n<!-- Decision: not required -->\nRationale: All contents remain static.\n',
    'rationale': '## Governance and security\nDecision: not required\n<!-- Rationale: All contents remain static. -->\n',
    'unclosed_decision': '## Governance and security\n<!--\nDecision: not required\nRationale: All contents remain static.\n',
    'unclosed_rationale': '## Governance and security\nDecision: not required\n<!--\nRationale: All contents remain static.\n',
    'rationale_before_fence': '## Governance and security\nDecision: not required\nRationale: <!-- All contents remain static. -->\n```md\nRationale: An example cannot supply this.\n```\n',
    'comment_cannot_move_heading_to_column_zero': '<!-- hidden -->'+GOVERNANCE,
    'comment_cannot_move_decision_to_column_zero': GOVERNANCE.replace('Decision:', '<!-- hidden -->Decision:'),
    'comment_cannot_move_rationale_to_column_zero': GOVERNANCE.replace('Rationale:', '<!-- hidden -->Rationale:'),
}
COLUMN_ZERO_GOVERNANCE = {}
for prefix in (' ', '  ', '   ', '    ', '\t', '> ', '- ', '* ', '+ ', '1. ', '1) ', '>', '-', '*', '+', '1.', '1)', '>1)-'):
    for label in ('## Governance', 'Decision:', 'Rationale:'):
        COLUMN_ZERO_GOVERNANCE[repr(prefix)+'_'+label] = GOVERNANCE.replace(label, prefix+label)
    COLUMN_ZERO_GOVERNANCE[repr(prefix)+'_inline'] = '## Governance and security\n'+prefix+'Not required because all contents remain static.\n'
COLUMN_ZERO_GOVERNANCE.update({
    'indented_inline_rationale': '## Governance and security\nDecision: not required\n  All contents remain static.\n',
    'quoted_inline_rationale': '## Governance and security\nDecision: not required\n> All contents remain static.\n',
    'listed_inline_rationale': '## Governance and security\nDecision: not required\n- All contents remain static.\n',
    'access_fields_without_rationale_line': '## Governance and security\nDecision: required\n- Access: Restrict access to credentials.\n- Logging: Redact personal data.\n',
})
GOVERNANCE_FIRST.update({
    'unclosed_comment_after_section': GOVERNANCE+'<!--\n```md\n',
    'inline_comment_in_rationale': GOVERNANCE.replace('This documentation', 'This <!-- hidden --> documentation'),
    'commented_placeholder': GOVERNANCE.replace('Rationale:', 'Rationale: <!-- TBD -->'),
    'only_two_delimiters': '``md\n~~md\n'+GOVERNANCE+'```md\nexample\n```\n',
    'inline_fence_text': 'Prose mentions ```inline``` markers.\n'+GOVERNANCE,
})

# Preserve every earlier root-fence rejection through the real adapter too.
FENCED_GOVERNANCE.update({
    'earlier_root_fence_7': '   ````md\n   ```\n## Governance and security\nDecision: not required\nRationale: This documentation change handles no credentials or personal data.\n   ````\n',
    'earlier_root_fence_8': '   ~~~~md\n   ~~~\n## Governance and security\nDecision: not required\nRationale: This documentation change handles no credentials or personal data.\n   ~~~~\n',
    'earlier_root_fence_12': '    ```md\n## Governance and security\nDecision: not required\nRationale: This documentation change handles no credentials or personal data.\n',
    'earlier_root_fence_13': '    ~~~md\n## Governance and security\nDecision: not required\nRationale: This documentation change handles no credentials or personal data.\n',
})

# Round 8: physical lines, normalized values and simple Markdown cutoffs.
ROUND8_INVALID = {
    'html_style': '<style>\n'+GOVERNANCE+'</style>\n',
    'html_hidden_div': '  <div hidden>\n'+GOVERNANCE+'</div>\n',
    'html_declaration': '<!DOCTYPE html>\n'+GOVERNANCE,
    'html_closing_tag': '</div>\n'+GOVERNANCE,
    # These two previously qualified; <! now establishes the HTML cutoff.
    'commented_code_before_section': '<!--\n> 1) - ```md\n~~~\n-->\n'+GOVERNANCE,
    'multiple_comments_before_section': '<!-- hidden --><!-- more -->\n'+GOVERNANCE,
    'yaml_hidden_section': '---\n'+GOVERNANCE+'---\n# Feature\n',
    'yaml_unclosed': '---\n'+GOVERNANCE,
    'code_span_decision': GOVERNANCE.replace('not required', '`not required`'),
    'code_span_rationale': GOVERNANCE.replace('Rationale: ', 'Rationale: `')+'`\n',
    'code_span_then_prose': GOVERNANCE.replace('Rationale: ', 'Rationale: `Static` '),
    'code_span_fields': GOVERNANCE.replace('Decision:', '`Decision:').replace('Rationale:', '`\n`Rationale:')+'`\n',
    'code_span_multiline': GOVERNANCE.replace('Rationale: ', 'Rationale:\n`')+'`\n',
    'indented_rationale': GOVERNANCE.replace('Rationale: ', 'Rationale:\n\n    '),
    'tabbed_rationale': GOVERNANCE.replace('Rationale: ', 'Rationale:\n\t'),
    'mixed_indent_rationale': GOVERNANCE.replace('Rationale: ', 'Rationale:\n \t'),
    'setext_dash_boundary': '## Governance and security\n\nExample\n-------\n'+GOVERNANCE.split('\n', 1)[1],
    'setext_equals_boundary': '## Governance and security\n\nExample\n===\n'+GOVERNANCE.split('\n', 1)[1],
    'setext_rationale_is_heading': GOVERNANCE+'---\n',
    'heading_nbsp': GOVERNANCE.replace('## ', '##\u00a0'),
    'heading_tab': GOVERNANCE.replace('## ', '##\t'),
}
for separator in ('\v', '\f', '\x1c', '\x1d', '\x1e', '\x85', '\u2028', '\u2029', '\r'):
    ROUND8_INVALID['synthetic_lines_'+hex(ord(separator))] = '    '+separator+GOVERNANCE.rstrip('\n').replace('\n', separator)+'\n'
for code in (*range(0, 32), *range(127, 160)):
    if code in (9, 10): continue
    for label in ('## Governance and security', 'Decision: not required', 'Rationale:'):
        ROUND8_INVALID['control_'+hex(code)+'_'+label] = GOVERNANCE.replace(label, label+chr(code)+' ')
for value in ('\u200b', '&#8203;', '&ZeroWidthSpace;', '\u2060\ufeff', 'T\u200bBD',
              'T&#8203;BD', 'ＴＢＤ', 'ＴＯＤＯ', 'ｎ／ａ', '&#84;BD', '-', '…', '?!', '💭'):
    ROUND8_INVALID['normalized_'+repr(value)] = GOVERNANCE.split('Rationale:')[0]+'Rationale: '+value+'\n'

ROUND8_VALID = {
    'crlf': GOVERNANCE.replace('\n', '\r\n'),
    'yaml_then_real_section': '---\ntitle: Feature\n---\n'+GOVERNANCE,
    'governance_before_html': GOVERNANCE+'<div hidden>example</div>\n',
    'normalized_values': GOVERNANCE.replace('not required', 'ｎｏｔ required').replace('This documentation', 'Ｔhis docu\u200bmentation'),
    'normalized_heading_text': GOVERNANCE.replace('Governance and security', 'Ｇovernance and secu\u2060rity'),
    'heading_format_prefix': '\ufeff\u200b'+GOVERNANCE,
    'entity_rationale': GOVERNANCE.replace('This documentation', 'Th&#105;s documentation'),
    'unindented_continuation': GOVERNANCE.replace('Rationale: ', 'Rationale:\n'),
    'three_space_continuation': GOVERNANCE.replace('Rationale: ', 'Rationale:\n   '),
    'unicode_letters': GOVERNANCE.split('Rationale:')[0]+'Rationale: 静的文書\n',
    'digit': GOVERNANCE.split('Rationale:')[0]+'Rationale: 1\n',
    'unrelated_control': '# Feature\x01\n'+GOVERNANCE,
}
ROUND8_PROVIDER_INVALID = {name: ROUND8_INVALID[name] for name in ('html_hidden_div', "normalized_'&#8203;'")}

ROUND9_INVALID = {
    'parenthesized_tbd': GOVERNANCE.split('Rationale:')[0]+'Rationale: (TBD)\n',
    'bracketed_todo': GOVERNANCE.split('Rationale:')[0]+'Rationale: [TODO]\n',
    'emphasized_na': GOVERNANCE.split('Rationale:')[0]+'Rationale: *n/a*\n',
    'quoted_tbd': GOVERNANCE.split('Rationale:')[0]+'Rationale: "tbd"\n',
    'nested_wrapped_tbd': GOVERNANCE.split('Rationale:')[0]+'Rationale: ([“*TBD*”])?!\n',
    'inline_quote_tbd': GOVERNANCE.split('Rationale:')[0]+'Rationale: > TBD\n',
    'quoted_tbd_continuation': GOVERNANCE.split('Rationale:')[0]+'Rationale:\n> TBD\n',
    'quoted_prose_continuation': GOVERNANCE.replace('Rationale: ', 'Rationale:\n> '),
    'indented_code_then_governance': '    print("example")\n\n'+GOVERNANCE,
    'tabbed_code_then_governance': '\tprint("example")\n\n'+GOVERNANCE,
    'mixed_indent_code_after_blank': '# Feature\n\n \tprint("example")\n\n'+GOVERNANCE,
}
ROUND9_PROVIDER_INVALID = {name: ROUND9_INVALID[name] for name in (
    'parenthesized_tbd', 'bracketed_todo', 'quoted_tbd_continuation', 'indented_code_then_governance')}
ROUND9_VALID = {
    'yaml_then_real_section': ROUND8_VALID['yaml_then_real_section'],
    'governance_before_indented_code': GOVERNANCE+'\n    print("example")\n',
    'quoted_then_real_continuation': GOVERNANCE.replace('Rationale: ', 'Rationale:\n> TBD\n'),
}

# Final narrow follow-up: common template wording and quoted code/HTML cutoffs.
ROUND10_PLACEHOLDERS = {
    value: GOVERNANCE.split('Rationale:')[0]+'Rationale: '+value+'\n'
    for value in (
        *('To be '+verb for verb in ('completed', 'added', 'written', 'provided', 'confirmed', 'clarified', 'updated', 'done')),
        *('Will be '+verb+' later' for verb in ('added', 'provided', 'completed', 'documented')),
        'Noch zu ergänzen', 'Wird ergänzt', 'Wird nachgetragen',
        *('['+verb+' applicability]' for verb in ('Describe', 'Explain', 'Insert', 'Fill', 'Provide', 'Justify', 'Summarize', 'Specify', 'Beschreiben', 'Einfügen', 'Ergänzen')),
        '<Begründung einfügen>',
        *('TBD'+separator+'N/A' for separator in (' / ', '/', ', ', ' | ', '; ', ' or ', ' and ', ' oder ', ' und ')),
        'To be completed\n[Describe applicability]\nTBD / N/A',
    )
}
ROUND10_QUOTED_BLOCKS = {
    'quoted_indented_code': ('code', '>     print("example")\n\n'+GOVERNANCE),
    'nested_quoted_indented_code': ('code', '> >     print("example")\n\n'+GOVERNANCE),
    'quoted_html': ('HTML', '> <div>\n> example\n> </div>\n\n'+GOVERNANCE),
    'nested_quoted_html': ('HTML', '>> <div>\n\n'+GOVERNANCE),
}
ROUND10_PROVIDER_INVALID = {
    'template_deferral': ROUND10_PLACEHOLDERS['To be completed'],
    'quoted_indented_code': ROUND10_QUOTED_BLOCKS['quoted_indented_code'][1],
    'quoted_html': ROUND10_QUOTED_BLOCKS['quoted_html'][1],
}
ROUND10_VALID = {
    'real_slash_parts': GOVERNANCE.split('Rationale:')[0]+'Rationale: Encrypted at rest / access limited to the ops team\n',
    'placeholder_and_real_part': GOVERNANCE.split('Rationale:')[0]+'Rationale: TBD later / access limited to the ops team\n',
    'real_part_and_placeholder': GOVERNANCE.split('Rationale:')[0]+'Rationale: Encrypted at rest / N/A\n',
    'quoted_prose_before_governance': '> Plain quoted prose.\n> > Nested quoted prose.\n\n'+GOVERNANCE,
    'front_matter_then_governance': ROUND8_VALID['yaml_then_real_section'],
    'bracketed_write_prose': GOVERNANCE.split('Rationale:')[0]+'Rationale: [Write access is restricted to administrators]\n',
    'bracketed_list_prose': GOVERNANCE.split('Rationale:')[0]+'Rationale: [List entries contain no personal data]\n',
    'bracketed_add_prose': GOVERNANCE.split('Rationale:')[0]+'Rationale: [Add-on modules only read public data]\n',
    'short_bracketed_write_prose': GOVERNANCE.split('Rationale:')[0]+'Rationale: [Write operations fail]\n',
    'short_bracketed_list_prose': GOVERNANCE.split('Rationale:')[0]+'Rationale: [List entries expire]\n',
}
