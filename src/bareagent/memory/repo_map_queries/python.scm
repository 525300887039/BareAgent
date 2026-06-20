; Repo-map tags for Python. Capture convention:
;   @name              the relevant identifier (symbol name for defs, callee for refs)
;   @definition.<kind> the whole definition node (its range drives nesting + signature)
;   @reference.<kind>  a use site (drives the PageRank reference graph)
(class_definition name: (identifier) @name) @definition.class
(function_definition name: (identifier) @name) @definition.function
(call
  function: [
    (identifier) @name
    (attribute attribute: (identifier) @name)
  ]) @reference.call
