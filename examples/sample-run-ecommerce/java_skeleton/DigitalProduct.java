public class DigitalProduct extends Product {
    private double fileSize;

    public DigitalProduct(String name, double price, double fileSize) {
        super(name, price);
        this.fileSize = fileSize;
    }

    public double getFileSize() {
        return this.fileSize;
    }
    public void setFileSize(double fileSize) {
        this.fileSize = fileSize;
    }

    public void download() {
    }
}