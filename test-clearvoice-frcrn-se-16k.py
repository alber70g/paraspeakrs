from clearvoice import ClearVoice

# modelname="FRCRN_SE_16K"
modelname="MossFormerGAN_SE_16K"
# Initialize with the lightest model (FRCRN_SE_16K)
cv = ClearVoice(
    task="speech_enhancement",  # not "se"
    model_names=[modelname],  # exact model name
)

# Run on your file (first run will auto-download the model ~200-300 MB)
output_audio = cv(
    input_path="test-audio/albert-gertjan-snippet-18-20.wav",
    online_write=False,
)

# Save the cleaned result
cv.write(
    output_audio, output_path="out-clearvoice/albert-gertjan-snippet-18-20-clean.wav"
)

print("✅ Done! Check out-clearvoice/albert-gertjan-snippet-18-20-clean.wav")

