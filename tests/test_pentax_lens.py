from photoapp import pentax_lens


def test_known_codes_decode_to_lens_names():
  # ticket 165: a few entries straight from ExifTool's table (the codes the real library uses).
  assert pentax_lens.decode(8, 215) == "smc PENTAX-DA 18-135mm F3.5-5.6 ED AL [IF] DC WR"
  assert pentax_lens.decode(4, 252) == "smc PENTAX-DA 18-55mm F3.5-5.6 AL"
  assert pentax_lens.decode(0, 0) == "M-42 or No Lens"
  assert pentax_lens.decode(1, 0) == "K or M Lens"


def test_series_fallbacks():
  # ticket 165: ExifTool's OTHER remaps for firmware that reports a neighbouring series.
  assert pentax_lens.decode(4, 224) == "smc PENTAX-DA 15mm F4 ED AL Limited (4 224)"
  assert pentax_lens.decode(7, 11) == "Sigma 10-20mm F3.5 EX DC HSM ? (7 11)"
  assert pentax_lens.decode(13, 1) == "smc PENTAX-FA 645 75mm F2.8 ? (13 1)"


def test_unknown_code_is_none():
  assert pentax_lens.decode(99, 99) is None
