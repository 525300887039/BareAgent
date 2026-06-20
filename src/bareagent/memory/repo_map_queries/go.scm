; Repo-map tags for Go. See python.scm for the capture convention.
(function_declaration name: (identifier) @name) @definition.function
(method_declaration name: (field_identifier) @name) @definition.method
(type_declaration (type_spec name: (type_identifier) @name)) @definition.class
(call_expression
  function: [
    (identifier) @name
    (selector_expression field: (field_identifier) @name)
  ]) @reference.call
