; Exported from sea-mlir test/VC/mul-overflow.mlir with sea-vc --smt2.
; Asserts a counterexample to signed i8 multiplication having no overflow.
; Expected: sat. Uses Z3 native multiplication-overflow predicates.
(declare-fun ^bb0_0 () Bool)
(declare-fun nd.0_0 () (_ BitVec 8))
(declare-fun nd.1_0 () (_ BitVec 8))

(assert (let ((a!1 (ite (not (and (bvsmul_noovfl nd.0_0 nd.1_0)
                          (bvsmul_noudfl nd.0_0 nd.1_0)))
                #b1
                #b0)))
  (and ^bb0_0 (not (= (bvxor a!1 #b1) #b1)))))
(check-sat)
